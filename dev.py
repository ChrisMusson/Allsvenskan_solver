import glob
import os
import random
import string
import subprocess
import threading
import time
import warnings
from itertools import product
from pathlib import Path
from subprocess import Popen

import numpy as np
import requests
import sasoptpy as so

warnings.simplefilter(action="ignore", category=FutureWarning)
import pandas as pd  # noqa: E402

BASE_URL = "https://en.fantasy.allsvenskan.se/api"


def get_random_id(n):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def xmin_to_prob(xmin, sub_on=0.5, sub_off=0.3):
    start = min(max((xmin - 25 * sub_on) / (90 * (1 - sub_off) + 65 * sub_off - 25 * sub_on), 0.001), 0.999)
    return start + (1 - start) * sub_on


def get_dict_combinations(my_dict):
    keys = my_dict.keys()
    for key in keys:
        if my_dict[key] is None or len(my_dict[key]) == 0:
            my_dict[key] = [None]
    all_combs = [dict(zip(my_dict.keys(), values)) for values in product(*my_dict.values())]
    feasible_combs = []
    for comb in all_combs:
        comb_copy = comb.copy()
        c_values = [i for i in comb_copy.values() if i is not None]
        if len(c_values) == len(set(c_values)):
            feasible_combs.append(comb)
        # else we have a duplicate
    return feasible_combs


def generate_team_json(team_id, options):
    price_changes = {pid: change for pid, change in options.get("price_changes", [])}
    with requests.Session() as session:
        static_url = f"{BASE_URL}/bootstrap-static/"
        static = session.get(static_url).json()
        element_to_type_dict = {x["id"]: x["element_type"] for x in static["elements"]}
        next_gw = [x for x in static["events"] if x["is_next"]][0]["id"]

        start_prices = {x["id"]: x["now_cost"] - x["cost_change_start"] for x in static["elements"]}
        gw1_url = f"{BASE_URL}/entry/{team_id}/event/1/picks/"
        gw1 = session.get(gw1_url).json()

        transfers_url = f"{BASE_URL}/entry/{team_id}/transfers/"
        transfers = session.get(transfers_url).json()[::-1]

        chips_url = f"{BASE_URL}/entry/{team_id}/history/"
        chips = session.get(chips_url).json()["chips"]
        lr = [x for x in chips if x["name"] == "uteam"]
        if lr:
            lr = lr[0]["event"]
        wc_gws = [x["event"] for x in chips if x["name"] == "wildcard"]

    # squad will remain an ID:puchase_price map throughout iteration over transfers
    # once they have been iterated through, can then add on the current selling price
    squad = {x["element"]: start_prices[x["element"]] for x in gw1["picks"]}
    for i, chip in enumerate(chips):
        chips[i]["played_by_entry"] = [chip["event"]]
        del chips[i]["event"]

    transfers = sorted(transfers, key=lambda x: x["event"])

    itb = 1000 - sum(squad.values())
    for t in transfers:
        if t["event"] == lr:
            continue
        itb += t["element_out_cost"]
        itb -= t["element_in_cost"]
        if t["element_in"]:
            squad[t["element_in"]] = t["element_in_cost"]
        if t["element_out"]:
            del squad[t["element_out"]]

    fts = calculate_fts(transfers, next_gw, lr, wc_gws)
    my_data = {
        "chips": chips,
        "picks": [],
        "team_id": team_id,
        "transfers": {
            "bank": itb,
            "limit": fts,
            "made": 0,
        },
    }
    for player_id, purchase_price in squad.items():
        now_cost = [x for x in static["elements"] if x["id"] == player_id][0]["now_cost"]
        if player_id in price_changes:
            now_cost += price_changes[player_id]

        diff = now_cost - purchase_price
        if diff > 0:
            selling_price = purchase_price + diff // 2
        else:
            selling_price = now_cost

        my_data["picks"].append(
            {"element": player_id, "purchase_price": purchase_price, "selling_price": selling_price, "element_type": element_to_type_dict[player_id]}
        )

    return my_data


def calculate_fts(transfers, next_gw, lr, wc_gws):
    n_transfers = {gw: 0 for gw in range(2, next_gw)}
    for t in transfers:
        n_transfers[t["event"]] += 1
    fts = {gw: 0 for gw in range(2, next_gw + 1)}
    fts[2] = 1
    for i in range(3, next_gw + 1):
        if (i - 1) == lr:
            fts[i] = fts[i - 1]
            continue
        if i - 1 in wc_gws:
            fts[i] = fts[i - 1]
            continue
        fts[i] = fts[i - 1]
        fts[i] -= n_transfers[i - 1]
        fts[i] = max(fts[i], 0)
        fts[i] += 1
        fts[i] = min(fts[i], 5)
    return fts[next_gw]


def prep_data(my_data, options):
    r = requests.get(f"{BASE_URL}/bootstrap-static/")
    fpl_data = r.json()
    valid_ids = [x["id"] for x in fpl_data["elements"]]

    for pid, change in options.get("price_changes", []):
        if pid not in valid_ids:
            continue
        player = [x for x in fpl_data["elements"] if x["id"] == pid][0]
        player["now_cost"] += change

    if options.get("override_next_gw", None):
        gw = int(options["override_next_gw"])
    else:
        gw = 1
        for e in fpl_data["events"]:
            if e["is_next"]:
                gw = e["id"]
                break

    horizon = options.get("horizon", 3)

    element_data = pd.DataFrame(fpl_data["elements"])
    team_data = pd.DataFrame(fpl_data["teams"])
    elements_team = pd.merge(element_data, team_data, left_on="team", right_on="id")

    datasource = options.get("datasource", "vamps")

    # data = read_data(options, datasource, data_weights)
    # data = pd.read_csv("data/vamps.csv")
    datasource = options.get("datasource")
    if datasource:
        data = pd.read_csv(f"data/{datasource}.csv")

    else:
        list_of_files = glob.glob("data/*.csv")
        try:
            latest_file = max(list_of_files, key=os.path.getctime)
            print(f"Using prediction file: {latest_file}")
            data = pd.read_csv(latest_file)
        except Exception as e:
            print("Cannot find prediction data in the folder! Upload it to /data/ and make sure it is a .csv file")
            exit(0)

    data = data.fillna(0)
    if "ID" in data:
        data["review_id"] = data["ID"]
    else:
        data["review_id"] = data.index + 1

    if options.get("export_data", "") != "" and datasource == "mixed":
        data.to_csv(f"data/{options['export_data']}")

    # Type fixes due to AM projections
    data["review_id"] = data["review_id"].astype(np.int64)
    for col in data.columns:
        if "_xMins" in col:
            data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0).astype(int)

    merged_data = pd.merge(elements_team, data, left_on="id_x", right_on="review_id")
    merged_data.set_index(["id_x"], inplace=True)

    # Drop duplicates
    merged_data = merged_data.drop_duplicates(subset=["ID"], keep="first")

    # Check if data exists
    for week in range(gw, min(39, gw + horizon)):
        if f"{week}_Pts" not in data.keys():
            raise ValueError(f"{week}_Pts is not inside prediction data, change your horizon parameter or update your prediction data")

    original_keys = merged_data.columns.to_list()
    keys = [k for k in original_keys if "_Pts" in k]
    min_keys = [k for k in original_keys if "_xMins" in k]
    merged_data["total_ev"] = merged_data[keys].sum(axis=1)
    merged_data["total_min"] = merged_data[min_keys].sum(axis=1)

    merged_data.sort_values(by=["total_ev"], ascending=[False], inplace=True)

    locked_next_gw = [int(i[0]) if isinstance(i, list) else int(i) for i in options.get("locked_next_gw", [])]
    safe_players_due_price = []
    for pos, vals in options.get("pick_prices", {}).items():
        if vals is None or vals == "":
            continue
        price_vals = [float(i) for i in vals.split(",")]
        pp = merged_data[(merged_data["Pos"] == pos) & ((merged_data["now_cost"] / 10).isin(price_vals))]["review_id"].to_list()
        safe_players_due_price += pp

    # Filter players by total EV
    cutoff = merged_data["total_ev"].quantile((100 - options.get("keep_top_ev_percent", 10)) / 100)
    safe_players_due_ev = merged_data[(merged_data["total_ev"] > cutoff)]["review_id"].tolist()

    initial_squad = [int(i["element"]) for i in my_data["picks"]]

    safe_players = initial_squad + options.get("locked", []) + options.get("keep", []) + locked_next_gw + safe_players_due_price + safe_players_due_ev

    # Filter players by xMin
    xmin_lb = options.get("xmin_lb", 1)
    print(len(merged_data), "total players (before)")
    merged_data = merged_data[(merged_data["total_min"] >= xmin_lb) | (merged_data["review_id"].isin(safe_players))].copy()

    # Filter by ev per price
    ev_per_price_cutoff = options.get("ev_per_price_cutoff", 0)
    for bt in options.get("booked_transfers", []):
        if bt.get("transfer_in"):
            safe_players.append(bt["transfer_in"])
        if bt.get("transfer_out"):
            safe_players.append(bt["transfer_out"])
    if ev_per_price_cutoff != 0:
        cutoff = (merged_data["total_ev"] / merged_data["now_cost"]).quantile(ev_per_price_cutoff / 100)
        merged_data = merged_data[(merged_data["total_ev"] / merged_data["now_cost"] > cutoff) | (merged_data["review_id"].isin(safe_players))].copy()

    print(len(merged_data), "total players (after)")

    if options.get("randomized", False):
        rng = np.random.default_rng(seed=options.get("seed"))
        gws = list(range(gw, min(39, gw + horizon)))
        for w in gws:
            noise = merged_data[f"{w}_Pts"] * (92 - merged_data[f"{w}_xMins"]) / 134 * rng.standard_normal(size=len(merged_data))
            merged_data[f"{w}_Pts"] = merged_data[f"{w}_Pts"] + noise

    type_data = pd.DataFrame(fpl_data["element_types"]).set_index(["id"])

    buy_price = (merged_data["now_cost"] / 10).to_dict()
    sell_price = {i["element"]: i["selling_price"] / 10 for i in my_data["picks"]}
    price_modified_players = []

    preseason = options.get("preseason", False)
    if not preseason:
        for i in my_data["picks"]:
            if i["element_type"] == 5:
                continue
            if buy_price[i["element"]] != sell_price[i["element"]]:
                price_modified_players.append(i["element"])
                print(f"Added player {i['element']} to list, buy price {buy_price[i['element']]} sell price {sell_price[i['element']]}")

    itb = my_data["transfers"]["bank"] / 10
    ft_base = None
    if my_data["transfers"]["limit"] is None:
        ft = 1
        ft_base = 1
    else:
        ft = my_data["transfers"]["limit"] - my_data["transfers"]["made"]
        ft_base = my_data["transfers"]["limit"]

    if ft < 0:
        ft = 0

    # If wildcard is active, then you have: "status_for_entry": "active" under my_data['chips']
    # can only pass the check when using "team_data": "json"
    for c in my_data["chips"]:
        if c["name"] == "wildcard" and c.get("status_for_entry", "") == "active":
            options["use_wc"] = gw
            if options["chip_limits"]["wc"] == 0:
                options["chip_limits"]["wc"] = 1
            break

    # Fixture info
    team_code_dict = team_data.set_index("id")["name"].to_dict()
    fixture_data = requests.get(f"{BASE_URL}/fixtures/").json()
    fixtures = [{"gw": f["event"], "home": team_code_dict[f["team_h"]], "away": team_code_dict[f["team_a"]]} for f in fixture_data]

    return {
        "merged_data": merged_data,
        "team_data": team_data,
        "my_data": my_data,
        "type_data": type_data,
        "next_gw": gw,
        "initial_squad": initial_squad,
        "sell_price": sell_price,
        "buy_price": buy_price,
        "price_modified_players": price_modified_players,
        "itb": itb,
        "ft": ft,
        "ft_base": ft_base,
        "fixtures": fixtures,
    }


def solve_multi_period_fpl(data, options):
    """
    Solves multi-objective FPL problem with transfers

    Parameters
    ----------
    data: dict
        Pre-processed data for the problem definition
    options: dict
        User controlled values for the problem instance
    """

    try:
        commit_hash = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        commit_count = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        version = f"{commit_count} - {commit_hash}"
        print(f"Version: {version}")
    except Exception:
        pass

    # Arguments
    problem_id = get_random_id(5)
    horizon = options.get("horizon", 3)
    objective = options.get("objective", "decay")
    decay_base = options.get("decay_base", 0.84)
    bench_weights = options.get("bench_weights", {0: 0.03, 1: 0.21, 2: 0.06, 3: 0.002})
    bench_weights = {int(key): value for (key, value) in bench_weights.items()}
    # wc_limit = options.get('wc_limit', 0)
    ft_value = options.get("ft_value", 1.5)
    ft_value_list = options.get("ft_value_list", dict())
    # ft_gw_value = {}
    ft_use_penalty = options.get("ft_use_penalty", None)
    itb_value = options.get("itb_value", 0.08)
    ft = data.get("ft", 1)
    ft_base = data.get("ft_base", 1)
    if ft <= 0:
        ft = 0
    chip_limits = options.get("chip_limits", dict())
    allowed_chip_gws = options.get("allowed_chip_gws", dict())
    forced_chip_gws = options.get("forced_chip_gws", dict())
    run_chip_combinations = options.get("run_chip_combinations", None)
    booked_transfers = options.get("booked_transfers", [])
    preseason = options.get("preseason", False)
    itb_loss_per_transfer = options.get("itb_loss_per_transfer", None)
    if itb_loss_per_transfer is None:
        itb_loss_per_transfer = 0
    weekly_hit_limit = options.get("weekly_hit_limit", None)

    # Data
    problem_name = f"mp_h{horizon}_regular" if objective == "regular" else f"mp_h{horizon}_o{objective[0]}_d{decay_base}"
    merged_data = data["merged_data"]
    team_data = data["team_data"]
    type_data = data["type_data"]
    next_gw = data["next_gw"]
    initial_squad = data["initial_squad"]
    itb = data["itb"]
    fixtures = data["fixtures"]
    if preseason:
        itb = 100
        threshold_gw = 2
    else:
        threshold_gw = next_gw

    # Sets
    players = merged_data.index.to_list()
    element_types = type_data.index.to_list()
    teams = team_data["name"].to_list()
    last_gw = next_gw + horizon - 1
    if last_gw > 38:
        last_gw = 38
        horizon = 39 - next_gw
    gameweeks = list(range(next_gw, last_gw + 1))
    all_gw = [next_gw - 1] + gameweeks
    order = [0, 1, 2, 3]
    price_modified_players = data["price_modified_players"]
    ft_states = [0, 1, 2, 3, 4, 5]

    # Model
    model = so.Model(name=problem_name)

    # Variables
    squad = model.add_variables(players, all_gw, name="squad", vartype=so.binary)
    squad_lr = model.add_variables(players, gameweeks, name="squad_lr", vartype=so.binary)
    lineup = model.add_variables(players, gameweeks, name="lineup", vartype=so.binary)
    captain = model.add_variables(players, gameweeks, name="captain", vartype=so.binary)
    vicecap = model.add_variables(players, gameweeks, name="vicecap", vartype=so.binary)
    bench = model.add_variables(players, gameweeks, order, name="bench", vartype=so.binary)
    transfer_in = model.add_variables(players, gameweeks, name="transfer_in", vartype=so.binary)
    # transfer_out = model.add_variables(players, gameweeks, name='transfer_out', vartype=so.binary)
    transfer_out_first = model.add_variables(price_modified_players, gameweeks, name="tr_out_first", vartype=so.binary)
    transfer_out_regular = model.add_variables(players, gameweeks, name="tr_out_reg", vartype=so.binary)
    transfer_out = {
        (p, w): transfer_out_regular[p, w] + (transfer_out_first[p, w] if p in price_modified_players else 0) for p in players for w in gameweeks
    }
    in_the_bank = model.add_variables(all_gw, name="itb", vartype=so.continuous, lb=0)
    free_transfers = model.add_variables(all_gw, name="ft", vartype=so.integer, lb=0, ub=5)
    ft_above_ub = model.add_variables(gameweeks, name="ft_over", vartype=so.binary)
    ft_below_lb = model.add_variables(gameweeks, name="ft_below", vartype=so.binary)
    free_transfers_state = model.add_variables(gameweeks, ft_states, name="ft_state", vartype=so.binary)
    penalized_transfers = model.add_variables(gameweeks, name="pt", vartype=so.integer, lb=0)
    aux = model.add_variables(gameweeks, name="aux", vartype=so.binary)
    transfer_count = model.add_variables(gameweeks, name="trc", vartype=so.integer, lb=0, ub=15)

    use_wc = model.add_variables(gameweeks, name="use_wc", vartype=so.binary)
    use_lr = model.add_variables(gameweeks, name="use_lr", vartype=so.binary)
    use_dd = model.add_variables(gameweeks, name="use_dd", vartype=so.binary)
    use_ptb = model.add_variables(gameweeks, name="use_ptb", vartype=so.binary)

    # Dictionaries
    lineup_type_count = {
        (t, w): so.expr_sum(lineup[p, w] for p in players if merged_data.loc[p, "element_type"] == t) for t in element_types for w in gameweeks
    }
    squad_type_count = {
        (t, w): so.expr_sum(squad[p, w] for p in players if merged_data.loc[p, "element_type"] == t) for t in element_types for w in gameweeks
    }
    squad_lr_type_count = {
        (t, w): so.expr_sum(squad_lr[p, w] for p in players if merged_data.loc[p, "element_type"] == t) for t in element_types for w in gameweeks
    }
    player_type = merged_data["element_type"].to_dict()
    # player_price = (merged_data['now_cost'] / 10).to_dict()
    sell_price = data["sell_price"]
    buy_price = data["buy_price"]
    sold_amount = {
        w: so.expr_sum(sell_price[p] * transfer_out_first[p, w] for p in price_modified_players)
        + so.expr_sum(buy_price[p] * transfer_out_regular[p, w] for p in players)
        for w in gameweeks
    }
    bought_amount = {w: so.expr_sum(buy_price[p] * transfer_in[p, w] for p in players) for w in gameweeks}
    points_player_week = {(p, w): merged_data.loc[p, f"{w}_Pts"] for p in players for w in gameweeks}
    minutes_player_week = {(p, w): merged_data.loc[p, f"{w}_xMins"] for p in players for w in gameweeks}
    player_team = {p: merged_data.loc[p, "name"] for p in players}
    squad_count = {w: so.expr_sum(squad[p, w] for p in players) for w in gameweeks}
    squad_lr_count = {w: so.expr_sum(squad_lr[p, w] for p in players) for w in gameweeks}
    number_of_transfers = {w: so.expr_sum(transfer_out[p, w] for p in players) for w in gameweeks}
    # number_of_transfers[next_gw-1] = 1
    transfer_diff = {w: number_of_transfers[w] - free_transfers[w] - 15 * use_wc[w] for w in gameweeks}
    # if weekly_hit_limit is not None:
    #     penalized_transfers.set_bounds(ub=weekly_hit_limit)

    # Chip combinations
    if run_chip_combinations is not None:
        chip_combinations = get_dict_combinations(run_chip_combinations)

        if len(chip_combinations) > 0:
            if set(chip_combinations[0].values()) == {None}:
                pass  # No possible chip combination
            else:
                print("You have active chip combinations, iteration parameter will be overridden")
                options["iteration"] = len(chip_combinations)
                options["iteration_criteria"] = "chip_combinations"

                current_chips = chip_combinations[0]
                pairs = [
                    {"chip": "wc", "variable": use_wc},
                    {"chip": "lr", "variable": use_lr},
                    {"chip": "dd", "variable": use_dd},
                    {"chip": "ptb", "variable": use_ptb},
                ]
                for pair in pairs:
                    chip = pair["chip"]
                    variable = pair["variable"]
                    cc_gw = current_chips.get(chip)
                    if cc_gw is not None and cc_gw in gameweeks:
                        model.add_constraint(variable[cc_gw] == 1, name=f"cc_{chip}")
                        options["chip_limits"][chip] = 1
                    else:
                        model.add_constraint(so.expr_sum(variable[w] for w in gameweeks) == 0, name=f"cc_{chip}")
                        options["chip_limits"][chip] = 0

    # Initial conditions
    model.add_constraints((squad[p, next_gw - 1] == 1 for p in initial_squad), name="initial_squad_players")
    model.add_constraints((squad[p, next_gw - 1] == 0 for p in players if p not in initial_squad), name="initial_squad_others")
    model.add_constraint(in_the_bank[next_gw - 1] == itb, name="initial_itb")
    model.add_constraint(free_transfers[next_gw] == ft * (1 - use_wc[next_gw]) + ft_base * use_wc[next_gw], name="initial_ft")
    model.add_constraints((free_transfers[w] >= 1 for w in gameweeks if w > next_gw), name="future_ft_limit")

    # Constraints
    model.add_constraints((squad_count[w] == 15 for w in gameweeks), name="squad_count")
    model.add_constraints((squad_lr_count[w] == 15 * use_lr[w] for w in gameweeks), name="squad_lr_count")
    model.add_constraints((so.expr_sum(lineup[p, w] for p in players) == 11 for w in gameweeks), name="lineup_count")
    model.add_constraints((so.expr_sum(bench[p, w, 0] for p in players if player_type[p] == 1) == 1 for w in gameweeks), name="bench_gk")
    model.add_constraints((so.expr_sum(bench[p, w, o] for p in players) == 1 for w in gameweeks for o in [1, 2, 3]), name="bench_count")
    model.add_constraints((so.expr_sum(captain[p, w] for p in players) <= 1 + use_dd[w] + 4 * use_ptb[w] for w in gameweeks), name="captain_count")
    model.add_constraints((so.expr_sum(vicecap[p, w] for p in players) == 1 - use_dd[w] - use_ptb[w] for w in gameweeks), name="vicecap_count")
    model.add_constraints((lineup[p, w] <= squad[p, w] + use_lr[w] for p in players for w in gameweeks), name="lineup_squad_rel")
    model.add_constraints((bench[p, w, o] <= squad[p, w] + use_lr[w] for p in players for w in gameweeks for o in order), name="bench_squad_rel")
    model.add_constraints((lineup[p, w] <= squad_lr[p, w] + 1 - use_lr[w] for p in players for w in gameweeks), name="lineup_squad_lr_rel")
    model.add_constraints(
        (bench[p, w, o] <= squad_lr[p, w] + 1 - use_lr[w] for p in players for w in gameweeks for o in order), name="bench_squad_lr_rel"
    )
    model.add_constraints((captain[p, w] <= lineup[p, w] for p in players for w in gameweeks), name="captain_lineup_rel")
    model.add_constraints(
        (captain[p, w] + use_ptb[w] <= 1 for p in players if player_type[p] != 2 for w in gameweeks), name="only_def_allowed_cap_ptb"
    )
    model.add_constraints((vicecap[p, w] <= lineup[p, w] for p in players for w in gameweeks), name="vicecap_lineup_rel")
    model.add_constraints((captain[p, w] + vicecap[p, w] <= 1 for p in players for w in gameweeks), name="cap_vc_rel")
    model.add_constraints(
        (lineup[p, w] + so.expr_sum(bench[p, w, o] for o in order) <= 1 for p in players for w in gameweeks), name="lineup_bench_rel"
    )
    model.add_constraints(
        (lineup_type_count[t, w] >= type_data.loc[t, "squad_min_play"] for t in element_types for w in gameweeks), name="valid_formation_lb"
    )
    model.add_constraints(
        (lineup_type_count[t, w] <= type_data.loc[t, "squad_max_play"] for t in element_types for w in gameweeks),
        name="valid_formation_ub",
    )
    model.add_constraints((squad_type_count[t, w] == type_data.loc[t, "squad_select"] for t in element_types for w in gameweeks), name="valid_squad")
    model.add_constraints(
        (squad_lr_type_count[t, w] == type_data.loc[t, "squad_select"] * use_lr[w] for t in element_types for w in gameweeks), name="valid_squad_lr"
    )
    model.add_constraints(
        (so.expr_sum(squad[p, w] for p in players if player_team[p] == t) <= 3 for t in teams for w in all_gw),
        name="team_limit",
    )
    model.add_constraints(
        (so.expr_sum(squad_lr[p, w] for p in players if player_team[p] == t) <= 11 * use_lr[w] for t in teams for w in gameweeks),
        name="team_limit_lr",
    )
    ## Transfer constraints
    model.add_constraints(
        (squad[p, w] == squad[p, w - 1] + transfer_in[p, w] - transfer_out[p, w] for p in players for w in gameweeks), name="squad_transfer_rel"
    )
    model.add_constraints(
        (
            in_the_bank[w]
            == in_the_bank[w - 1] + sold_amount[w] - bought_amount[w] - (transfer_count[w] * itb_loss_per_transfer if w > next_gw else 0)
            for w in gameweeks
        ),
        name="cont_budget",
    )

    lr_sell_price = {p: sell_price[p] if p in price_modified_players else buy_price[p] for p in players}
    model.add_constraints(
        (
            so.expr_sum(lr_sell_price[p] * squad[p, w - 1] for p in players) + in_the_bank[w - 1]
            >= so.expr_sum(lr_sell_price[p] * squad_lr[p, w] for p in players)
            for w in gameweeks
        ),
        name="lr_budget",
    )
    model.add_constraints((transfer_in[p, w] <= 1 - use_lr[w] for p in players for w in gameweeks), name="no_tr_in_lr")
    model.add_constraints((transfer_out[p, w] <= 1 - use_lr[w] for p in players for w in gameweeks), name="no_tr_out_lr")

    ## Free transfer constraints
    # 2024-2025 variation: min 1 / max 5 / roll over WC & lr
    raw_gw_ft = {w: free_transfers[w] - transfer_count[w] + 1 - use_wc[w] - use_lr[w] for w in gameweeks}
    model.add_constraints((free_transfers[w + 1] <= raw_gw_ft[w] + 16 * ft_below_lb[w] for w in gameweeks if w + 1 in gameweeks), name="newft1")
    model.add_constraints((free_transfers[w + 1] <= 1 + 4 * (1 - ft_below_lb[w]) for w in gameweeks if w + 1 in gameweeks), name="newft2")
    model.add_constraints(
        (free_transfers[w + 1] >= raw_gw_ft[w] - 2 * ft_above_ub[w] for w in gameweeks if w + 1 in gameweeks and w > 1), name="newft3"
    )
    model.add_constraints((free_transfers[w + 1] >= 5 - 5 * (1 - ft_above_ub[w]) for w in gameweeks if w + 1 in gameweeks and w > 1), name="newft4")

    model.add_constraints((free_transfers[w] == so.expr_sum(free_transfers_state[w, s] * s for s in ft_states) for w in gameweeks), name="ftsc1")
    model.add_constraints((so.expr_sum(free_transfers_state[w, s] for s in ft_states) == 1 for w in gameweeks), name="ftsc2")

    if preseason and threshold_gw in gameweeks:
        model.add_constraint(free_transfers[threshold_gw] == 1, name="ps_initial_ft")
    model.add_constraints((penalized_transfers[w] >= transfer_diff[w] for w in gameweeks), name="pen_transfer_rel")

    ## Chip constraints
    model.add_constraints((use_wc[w] + use_lr[w] + use_dd[w] + use_ptb[w] <= 1 for w in gameweeks), name="single_chip")
    model.add_constraints((aux[w] <= 1 - use_wc[w - 1] for w in gameweeks if w > next_gw), name="ft_after_wc")
    model.add_constraints((aux[w] <= 1 - use_lr[w - 1] for w in gameweeks if w > next_gw), name="ft_after_lr")

    if options.get("use_wc", None) is not None:
        model.add_constraint(use_wc[options["use_wc"]] == 1, name="force_wc")
        chip_limits["wc"] = 1
    if options.get("use_lr", None) is not None:
        model.add_constraint(use_lr[options["use_lr"]] == 1, name="force_lr")
        chip_limits["lr"] = 1
    if options.get("use_dd", None) is not None:
        model.add_constraint(use_dd[options["use_dd"]] == 1, name="force_dd")
        chip_limits["dd"] = 1
    if options.get("use_ptb", None) is not None:
        model.add_constraint(use_ptb[options["use_ptb"]] == 1, name="force_ptb")
        chip_limits["ptb"] = 1

    if len(allowed_chip_gws.get("wc", [])) > 0:
        gws_banned = [w for w in gameweeks if w not in allowed_chip_gws["wc"]]
        model.add_constraints((use_wc[w] == 0 for w in gws_banned), name="banned_wc_gws")
        chip_limits["wc"] = 1
    if len(allowed_chip_gws.get("lr", [])) > 0:
        gws_banned = [w for w in gameweeks if w not in allowed_chip_gws["lr"]]
        model.add_constraints((use_lr[w] == 0 for w in gws_banned), name="banned_lr_gws")
        chip_limits["lr"] = 1
    if len(allowed_chip_gws.get("dd", [])) > 0:
        gws_banned = [w for w in gameweeks if w not in allowed_chip_gws["dd"]]
        model.add_constraints((use_dd[w] == 0 for w in gws_banned), name="banned_dd_gws")
        chip_limits["dd"] = 1
    if len(allowed_chip_gws.get("ptb", [])) > 0:
        gws_banned = [w for w in gameweeks if w not in allowed_chip_gws["ptb"]]
        model.add_constraints((use_ptb[w] == 0 for w in gws_banned), name="banned_ptb_gws")
        chip_limits["ptb"] = 1

    if len(forced_chip_gws.get("wc", [])) > 0:
        model.add_constraint(so.expr_sum(use_wc[w] for w in forced_chip_gws["wc"]) == 1, name="force_wc_gw")
        chip_limits["wc"] = 1
    if len(forced_chip_gws.get("lr", [])) > 0:
        model.add_constraint(so.expr_sum(use_lr[w] for w in forced_chip_gws["lr"]) == 1, name="force_lr_gw")
        chip_limits["lr"] = 1
    if len(forced_chip_gws.get("dd", [])) > 0:
        model.add_constraint(so.expr_sum(use_dd[w] for w in forced_chip_gws["dd"]) == 1, name="force_dd_gw")
        chip_limits["dd"] = 1
    if len(forced_chip_gws.get("ptb", [])) > 0:
        model.add_constraint(so.expr_sum(use_ptb[w] for w in forced_chip_gws["ptb"]) == 1, name="force_ptb_gw")
        chip_limits["ptb"] = 1

    model.add_constraint(so.expr_sum(use_wc[w] for w in gameweeks) <= chip_limits.get("wc", 0), name="use_wc_limit")
    model.add_constraint(so.expr_sum(use_lr[w] for w in gameweeks) <= chip_limits.get("lr", 0), name="use_lr_limit")
    model.add_constraint(so.expr_sum(use_dd[w] for w in gameweeks) <= chip_limits.get("dd", 0), name="use_dd_limit")
    model.add_constraint(so.expr_sum(use_ptb[w] for w in gameweeks) <= chip_limits.get("ptb", 0), name="use_ptb_limit")

    model.add_constraints((squad_lr[p, w] <= use_lr[w] for p in players for w in gameweeks), name="lr_squad_logic")

    ## Multiple-sell fix
    model.add_constraints(
        (transfer_out_first[p, w] + transfer_out_regular[p, w] <= 1 for p in price_modified_players for w in gameweeks), name="multi_sell_1"
    )
    model.add_constraints(
        (
            horizon * so.expr_sum(transfer_out_first[p, w] for w in gameweeks if w <= wbar)
            >= so.expr_sum(transfer_out_regular[p, w] for w in gameweeks if w >= wbar)
            for p in price_modified_players
            for wbar in gameweeks
        ),
        name="multi_sell_2",
    )
    model.add_constraints((so.expr_sum(transfer_out_first[p, w] for w in gameweeks) <= 1 for p in price_modified_players), name="multi_sell_3")

    ## Transfer in/out fix
    model.add_constraints((transfer_in[p, w] + transfer_out[p, w] <= 1 for p in players for w in gameweeks), name="tr_in_out_limit")

    ## Tr Count Constraints
    ft_penalty = {w: 0 for w in gameweeks}
    model.add_constraints((transfer_count[w] >= number_of_transfers[w] - 15 * use_wc[w] for w in gameweeks), name="trc_lb")
    model.add_constraints((transfer_count[w] <= number_of_transfers[w] for w in gameweeks), name="trc_ub1")
    model.add_constraints((transfer_count[w] <= 15 * (1 - use_wc[w]) for w in gameweeks), name="trc_ub2")
    if ft_use_penalty is not None:
        ft_penalty = {w: ft_use_penalty * transfer_count[w] for w in gameweeks}

    ## Optional constraints
    if options.get("banned", None):
        print("OC - Banned")
        banned_players = options["banned"]
        model.add_constraints((so.expr_sum(squad[p, w] for w in gameweeks) == 0 for p in banned_players if p in players), name="ban_player")
        model.add_constraints((so.expr_sum(squad_lr[p, w] for w in gameweeks) == 0 for p in banned_players if p in players), name="ban_player_lr")

    if options.get("banned_next_gw", None):
        print("OC - Banned Next GW")
        banned_in_gw = [(x, gameweeks[0]) if isinstance(x, int) else tuple(x) for x in options["banned_next_gw"]]
        model.add_constraints((squad[p0, p1] == 0 for (p0, p1) in banned_in_gw if p0 in players), name="ban_player_specified_gw")

    if options.get("locked", None):
        print("OC - Locked")
        locked_players = options["locked"]
        model.add_constraints((squad[p, w] + squad_lr[p, w] == 1 for p in locked_players for w in gameweeks), name="lock_player")

    if options.get("locked_next_gw", None):
        print("OC - Locked Next GW")
        locked_in_gw = [(x, gameweeks[0]) if isinstance(x, int) else tuple(x) for x in options["locked_next_gw"]]
        model.add_constraints((squad[p0, p1] == 1 for (p0, p1) in locked_in_gw), name="lock_player_specified_gw")

    if options.get("locked_lr", None):
        print("OC - Locked LR")
        model.add_constraints((so.expr_sum(squad_lr[p, w] for w in gameweeks) == 1 for p in options.get("locked_lr")), name="locked_lr")

    if options.get("no_future_transfer", None):
        print("OC - No Future Tr")
        model.add_constraint(
            so.expr_sum(transfer_in[p, w] for p in players for w in gameweeks if w > next_gw and w != options.get("use_wc")) == 0,
            name="no_future_transfer",
        )

    if options.get("no_transfer_last_gws", None):
        print("OC - No TR last GWs")
        no_tr_gws = options["no_transfer_last_gws"]
        if horizon > no_tr_gws:
            model.add_constraints(
                (so.expr_sum(transfer_in[p, w] for p in players) <= 15 * use_wc[w] for w in gameweeks if w > last_gw - no_tr_gws), name="tr_ban_gws"
            )

    if options.get("num_transfers", None) is not None:
        print("OC - Num Transfers")
        model.add_constraint(so.expr_sum(transfer_in[p, next_gw] for p in players) == options["num_transfers"], name="tr_limit")

    if options.get("hit_limit", None):
        print("OC - Hit Limit")
        model.add_constraint(so.expr_sum(penalized_transfers[w] for w in gameweeks) <= int(options["hit_limit"]), name="horizon_hit_limit")

    if options.get("weekly_hit_limit") is not None:
        weekly_hit_limit = int(options.get("weekly_hit_limit"))
        model.add_constraints((penalized_transfers[w] <= weekly_hit_limit for w in gameweeks), name="gw_hit_lim")

    # if options.get("ft_custom_value", None) is not None:
    #     ft_custom_value = {int(key): value for (key, value) in options.get('ft_custom_value', {}).items()}
    #     ft_gw_value = {**{gw: ft_value for gw in gameweeks}, **ft_custom_value}

    if options.get("future_transfer_limit", None):
        print("OC - Future TR Limit")
        model.add_constraint(
            so.expr_sum(transfer_in[p, w] for p in players for w in gameweeks if w > next_gw and w != options.get("use_wc"))
            <= options["future_transfer_limit"],
            name="future_tr_limit",
        )

    if options.get("no_transfer_gws", None):
        print("OC - No TR GWs")
        if len(options["no_transfer_gws"]) > 0:
            model.add_constraint(so.expr_sum(transfer_in[p, w] for p in players for w in options["no_transfer_gws"]) == 0, name="banned_gws_for_tr")

    if options.get("no_transfer_by_position", None):
        print("OC - No TR by position")
        if len(options["no_transfer_by_position"]) > 0:
            # ignore w=1 as you must transfer in a full squad
            model.add_constraints(
                (
                    transfer_in[p, w] <= use_wc[w]
                    for p in players
                    for w in gameweeks
                    if w > 1
                    if merged_data.loc[p, "Pos"] in options["no_transfer_by_position"]
                ),
                name="no_tr_by_pos",
            )

    max_defs_per_team = options.get("max_defenders_per_team", 3)
    if max_defs_per_team < 3:  # only add constraints if necessary
        model.add_constraints(
            (
                so.expr_sum(squad[p, w] for p in players if player_team[p] == t and merged_data.loc[p, "Pos"] in {"G", "D"}) <= max_defs_per_team
                for t in teams
                for w in gameweeks
            ),
            name="defenders_per_team_limit",
        )
        model.add_constraints(
            (
                so.expr_sum(squad_lr[p, w] for p in players if player_team[p] == t and merged_data.loc[p, "Pos"] in {"G", "D"})
                <= max_defs_per_team * use_lr[w]
                for t in teams
                for w in gameweeks
            ),
            name="defenders_per_team_limit_lr",
        )

    for booked_transfer in booked_transfers:
        print("OC - Booked TRs")
        transfer_gw = booked_transfer.get("gw", None)

        if transfer_gw is None:
            continue

        player_in = booked_transfer.get("transfer_in", None)
        player_out = booked_transfer.get("transfer_out", None)

        if player_in is not None:
            model.add_constraint(transfer_in[player_in, transfer_gw] == 1, name=f"booked_transfer_in_{transfer_gw}_{player_in}")
        if player_out is not None:
            model.add_constraint(transfer_out[player_out, transfer_gw] == 1, name=f"booked_transfer_out_{transfer_gw}_{player_out}")

    cp_penalty = {}
    if options.get("no_opposing_play") is True:
        print("OC - No Opposing Play")
        gw_opp_teams = {
            w: [(f["home"], f["away"]) for f in fixtures if f["gw"] == w] + [(f["away"], f["home"]) for f in fixtures if f["gw"] == w]
            for w in gameweeks
        }
        for gw in gameweeks:
            # gw_games = [i for i in fixtures if i["gw"] == gw]
            if options.get("opposing_play_group", "all") == "all":
                opposing_players = [(p1, p2) for p1 in players for p2 in players if (player_team[p1], player_team[p2]) in gw_opp_teams[gw]]
                model.add_constraints((lineup[p1, gw] + lineup[p2, gw] <= 1 for (p1, p2) in opposing_players), name=f"no_opp_{gw}")
            elif options.get("opposing_play_group") == "position":
                opposing_positions = [(1, 3), (1, 4), (2, 3), (2, 4), (3, 1), (4, 1), (3, 2), (4, 2)]  # gk vs mid, gk vs fwd, def vs mid, def vs fwd
                opposing_players = [
                    (p1, p2)
                    for p1 in players
                    for p2 in players
                    if (player_team[p1], player_team[p2]) in gw_opp_teams[gw] and (player_type[p1], player_type[p2]) in opposing_positions
                ]
                model.add_constraints((lineup[p1, gw] + lineup[p2, gw] <= 1 for (p1, p2) in opposing_players), name=f"no_opp_{gw}")
    elif options.get("no_opposing_play") == "penalty":
        print("OC - Penalty Opposing Play")
        gw_opp_teams = {
            w: [(f["home"], f["away"]) for f in fixtures if f["gw"] == w] + [(f["away"], f["home"]) for f in fixtures if f["gw"] == w]
            for w in gameweeks
        }
        if options.get("opposing_play_group") == "all":
            cp_list = [
                (p1, p2, w)
                for p1 in players
                for p2 in players
                for w in gameweeks
                if (player_team[p1], player_team[p2]) in gw_opp_teams[w] and minutes_player_week[p1, w] > 0 and minutes_player_week[p2, w] > 0
            ]
        elif options.get("opposing_play_group", "position") == "position":
            opposing_positions = [(1, 3), (1, 4), (2, 3), (2, 4), (3, 1), (4, 1), (3, 2), (4, 2)]
            cp_list = [
                (p1, p2, w)
                for p1 in players
                for p2 in players
                for w in gameweeks
                if (player_team[p1], player_team[p2]) in gw_opp_teams[w]
                and minutes_player_week[p1, w] > 0
                and minutes_player_week[p2, w] > 0
                and (player_type[p1], player_type[p2]) in opposing_positions
            ]
        cp_pen_var = model.add_variables(cp_list, name="cp_v", vartype=so.binary)
        opposing_play_penalty = options.get("opposing_play_penalty", 0.5)
        cp_penalty = {w: opposing_play_penalty * so.expr_sum(cp_pen_var[p1, p2, w1] for (p1, p2, w1) in cp_list if w1 == w) for w in gameweeks}
        model.add_constraints((lineup[p1, w] + lineup[p2, w] <= 1 + cp_pen_var[p1, p2, w] for (p1, p2, w) in cp_list), name="cp1")
        model.add_constraints((cp_pen_var[p1, p2, w] <= lineup[p1, w] for (p1, p2, w) in cp_list), name="cp2")
        model.add_constraints((cp_pen_var[p1, p2, w] <= lineup[p2, w] for (p1, p2, w) in cp_list), name="cp3")

    if options.get("double_defense_pick") is True:
        print("OC - Double Defense Pick")
        team_players = {t: [p for p in players if player_team[p] == t] for t in teams}
        gk_df_players = {t: [p for p in team_players[t] if player_type[p] == 1 or player_type[p] == 2] for t in teams}
        weekly_sum = {(t, w): so.expr_sum(lineup[p, w] for p in gk_df_players[t]) for t in teams for w in gameweeks}
        def_aux = model.add_variables(teams, gameweeks, vartype=so.binary, name="daux")
        model.add_constraints((weekly_sum[t, w] <= 3 * def_aux[t, w] for t in teams for w in gameweeks), name="dauxc1")
        model.add_constraints((weekly_sum[t, w] >= 2 - 3 * (1 - def_aux[t, w]) for t in teams for w in gameweeks), name="dauxc2")

    if options.get("transfer_itb_buffer"):
        buffer_amount = float(options["transfer_itb_buffer"])
        gw_with_tr = model.add_variables(gameweeks, name="gw_with_tr", vartype=so.binary)
        model.add_constraints((15 * gw_with_tr[w] >= number_of_transfers[w] for w in gameweeks), name="gw_with_tr_lb")
        model.add_constraints((gw_with_tr[w] <= number_of_transfers[w] for w in gameweeks), name="gw_with_tr_ub")
        model.add_constraints((in_the_bank[w] >= buffer_amount * gw_with_tr[w] for w in gameweeks), name="buffer_con")

    if options.get("pick_prices", None) not in [None, {"G": "", "D": "", "M": "", "F": ""}]:
        print("OC - Pick Prices")
        buffer = 0.2
        price_choices = options["pick_prices"]
        for pos, val in price_choices.items():
            if val == "":
                continue
            price_points = [float(i) for i in val.split(",")]
            value_dict = {i: price_points.count(i) for i in set(price_points)}
            con_iter = 0
            for key, count in value_dict.items():
                target_players = [
                    p for p in players if merged_data.loc[p, "Pos"] == pos and buy_price[p] >= key - buffer and buy_price[p] <= key + buffer
                ]
                model.add_constraints(
                    (so.expr_sum(squad[p, w] for p in target_players) >= count for w in gameweeks), name=f"price_point_{pos}_{con_iter}"
                )
                con_iter += 1

    if options.get("no_gk_rotation_after", None):
        print("OC - No GK rotation")
        target_gw = int(options["no_gk_rotation_after"])
        players_gk = [p for p in players if player_type[p] == 1]
        model.add_constraints(
            (lineup[p, w] >= lineup[p, target_gw] - use_lr[w] for p in players_gk for w in gameweeks if w > target_gw), name="fixed_lineup_gk"
        )

    if len(options.get("no_chip_gws", [])) > 0:
        print("OC - No Chip GWs")
        no_chip_gws = options["no_chip_gws"]
        model.add_constraint(so.expr_sum(use_wc[w] + use_lr[w] + use_ptb[w] for w in no_chip_gws) == 0, name="no_chip_gws")

    if options.get("only_booked_transfers") is True:
        print("OC - Only Booked Transfers")
        forced_in = []
        forced_out = []
        for bt in options.get("booked_transfers", []):
            if bt["gw"] == next_gw:
                if bt.get("transfer_in") is not None:
                    forced_in.append(bt["transfer_in"])
                if bt.get("transfer_out") is not None:
                    forced_out.append(bt["transfer_out"])

        in_players = {(p): 1 if p in forced_in else 0 for p in players}
        out_players = {(p): 1 if p in forced_out else 0 for p in players}
        model.add_constraints((transfer_in[p, next_gw] == in_players[p] for p in players), name="fix_tgw_tr_in")
        model.add_constraints((transfer_out[p, next_gw] == out_players[p] for p in players), name="fix_tgw_tr_out")

    # if options.get('have_2ft_in_gws', None) is not None:
    #     for gw in options['have_2ft_in_gws']:
    #         model.add_constraint(free_transfers[gw] == 2, name=f'have_2ft_{gw}')

    if options.get("force_ft_state_lb", None):
        print("OC - Force FT LB")
        for gw, ft_pos in options["force_ft_state_lb"]:
            model.add_constraint(free_transfers[gw] >= ft_pos, name=f"cft_lb_{gw}")

    if options.get("force_ft_state_ub", None):
        print("OC - Force FT UB")
        for gw, ft_pos in options["force_ft_state_ub"]:
            model.add_constraint(free_transfers[gw] <= ft_pos, name=f"cft_ub_{gw}")

    if options.get("no_trs_except_wc", False) is True:
        print("OC - No TRS except WC")
        model.add_constraints((number_of_transfers[w] <= 15 * use_wc[w] for w in gameweeks), name="wc_trs_only")

    # FT gain
    ft_state_value = {}
    for s in ft_states:
        ft_state_value[s] = ft_state_value.get(s - 1, 0) + ft_value_list.get(str(s), ft_value)
    gw_ft_value = {w: so.expr_sum(ft_state_value[s] * free_transfers_state[w, s] for s in ft_states) for w in gameweeks}
    gw_ft_gain = {w: gw_ft_value[w] - gw_ft_value.get(w - 1, 0) for w in gameweeks}

    # Objectives
    hit_cost = options.get("hit_cost", 4)
    gw_xp = {
        w: so.expr_sum(
            points_player_week[p, w]
            * (lineup[p, w] + captain[p, w] + 0.1 * vicecap[p, w] + so.expr_sum(bench_weights[o] * bench[p, w, o] for o in order))
            for p in players
        )
        for w in gameweeks
    }
    gw_total = {
        w: gw_xp[w] - hit_cost * penalized_transfers[w] + gw_ft_gain[w] - ft_penalty[w] + itb_value * in_the_bank[w] - cp_penalty.get(w, 0)
        for w in gameweeks
    }

    if objective == "regular":
        total_xp = so.expr_sum(gw_total[w] for w in gameweeks)
        model.set_objective(-total_xp, sense="N", name="total_regular_xp")
    else:
        decay_objective = so.expr_sum(gw_total[w] * pow(decay_base, w - next_gw) for w in gameweeks)
        model.set_objective(-decay_objective, sense="N", name="total_decay_xp")

    report_decay_base = options.get("report_decay_base", [])
    decay_metrics = {i: so.expr_sum(gw_total[w] * pow(i, w - next_gw) for w in gameweeks) for i in report_decay_base}

    iteration = options.get("iteration", 1)
    iteration_criteria = options.get("iteration_criteria", "this_gw_transfer_in")
    solutions = []

    for iter in range(iteration):
        mps_file_name = f"tmp/{problem_name}_{problem_id}_{iter}.mps"
        sol_file_name = f"tmp/{problem_name}_{problem_id}_{iter}_sol.txt"
        opt_file_name = f"tmp/{problem_name}_{problem_id}_{iter}.opt"

        # Solve
        tmp_folder = Path() / "tmp"
        tmp_folder.mkdir(exist_ok=True, parents=True)
        model.export_mps(mps_file_name)
        print(f"Exported problem with name: {problem_name}_{problem_id}_{iter}")

        t0 = time.time()
        time.sleep(0.5)

        if options.get("export_debug", False) is True:
            with open("debug.sas", "w") as file:
                file.write(model.to_optmodel())

        use_cmd = options.get("use_cmd", False)
        solver = options.get("solver", "cbc")

        if solver == "cbc":
            cbc_exec = options.get("solver_path") or "cbc"

            if options.get("single_solve") is True:
                gap = options.get("gap", 0)
                secs = options.get("secs", 20 * 60)
                command = f"{cbc_exec} {mps_file_name} cost column ratio {gap} sec {secs} solve solu {sol_file_name}"
                if use_cmd:
                    os.system(command)
                else:
                    process = Popen(command, shell=False)
                    process.wait()

            else:
                command = f"{cbc_exec} {mps_file_name} cost column ratio 1 solve solu tmp/{problem_name}_{problem_id}_{iter}_sol_init.txt"
                if use_cmd:
                    os.system(command)
                else:
                    process = Popen(command, shell=False)
                    process.wait()
                secs = options.get("secs", 20 * 60)
                command = f"{cbc_exec} {mps_file_name} mips tmp/{problem_name}_{problem_id}_{iter}_sol_init.txt cost column sec {secs} solve solu {sol_file_name}"
                if use_cmd:
                    os.system(command)
                else:
                    process = Popen(command, shell=False)  # add 'stdout=DEVNULL' for disabling logs
                    process.wait()

            # Popen fix with split?

            t1 = time.time()
            print(t1 - t0, "seconds passed")

            # Parsing
            with open(sol_file_name, "r") as f:
                for v in model.get_variables():
                    v.set_value(0)
                for line in f:
                    words = line.split()
                    if words[0] == "Infeasible":
                        raise ValueError("Infeasible problem instance, check your parameters")
                    if "objective value" in line:
                        continue
                    var = model.get_variable(words[1])
                    var.set_value(float(words[2]))

        elif solver == "gurobi":
            gap = options.get("gap", 0)
            sol_file_name = sol_file_name.replace("_sol", "").replace("txt", "sol")
            command = f"gurobi_cl MIPGap={gap} ResultFile={sol_file_name} {mps_file_name}"

            if use_cmd:
                os.system(command)
            else:

                def print_output(process):
                    while True:
                        output = process.stdout.readline()
                        if "Solving report" in output:
                            time.sleep(2)
                            process.kill()
                        elif output == "" and process.poll() is not None:
                            break
                        elif output:
                            print(output.strip())

                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                output_thread = threading.Thread(target=print_output, args=(process,))
                output_thread.start()
                output_thread.join()

            # Parsing
            with open(sol_file_name, "r") as f:
                for v in model.get_variables():
                    v.set_value(0)
                cols_started = False
                for line in f:
                    if line[0] == "#":
                        continue
                    if line == "":
                        break
                    words = line.split()
                    v = model.get_variable(words[0])
                    try:
                        if v.get_type() == so.INT:
                            v.set_value(round(float(words[1])))
                        elif v.get_type() == so.BIN:
                            v.set_value(round(float(words[1])))
                        elif v.get_type() == so.CONT:
                            v.set_value(round(float(words[1]), 3))
                    except Exception:
                        print("Error", words[0], line)

        elif solver == "copt":
            sol_file_name = sol_file_name.replace("_sol", "").replace("txt", "sol")
            gap = options.get("gap", 0)
            command = f'copt_cmd -c "set RelGap {gap};readmps {mps_file_name};optimize;writesol {sol_file_name};quit"'
            if use_cmd:
                os.system(command)
            else:

                def print_output(process):
                    while True:
                        output = process.stdout.readline()
                        if "Solving report" in output:
                            time.sleep(2)
                            process.kill()
                        elif output == "" and process.poll() is not None:
                            break
                        elif output:
                            print(output.strip())

                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                output_thread = threading.Thread(target=print_output, args=(process,))
                output_thread.start()
                output_thread.join()

            # Parsing
            with open(sol_file_name, "r") as f:
                for v in model.get_variables():
                    v.set_value(0)

                for line in f:
                    if line == "":
                        break
                    if line[0] == "#":
                        continue
                    words = line.split()
                    v = model.get_variable(words[0])
                    try:
                        if v.get_type() == so.INT:
                            v.set_value(round(float(words[1])))
                        elif v.get_type() == so.BIN:
                            v.set_value(round(float(words[1])))
                        elif v.get_type() == so.CONT:
                            v.set_value(round(float(words[1]), 3))
                    except Exception:
                        print("Error", words[0], line)

        elif solver == "highs":
            highs_exec = options.get("solver_path") or "highs"

            secs = options.get("secs", 20 * 60)
            presolve = options.get("presolve", "on")
            gap = options.get("gap", 0)
            random_seed = options.get("random_seed", 0)

            with open(opt_file_name, "w") as f:
                f.write(f"""mip_rel_gap = {gap}""")
                # mip_improving_solution_file="tmp/{problem_id}_incumbent.sol"

            command = f"{highs_exec} --parallel on --options_file {opt_file_name} --random_seed {random_seed} --presolve {presolve} --model_file {mps_file_name} --time_limit {secs} --solution_file {sol_file_name}"

            if use_cmd:
                # highs occasionally freezes in Windows, if it happens, try use_cmd value as False
                # print('If you are using Windows, HiGHS occasionally freezes after solves are completed. Use \n\t"use_cmd": false\nin regular settings if it happens.')
                os.system(command)
            else:

                def print_output(process):
                    while True:
                        try:
                            output = process.stdout.readline()
                            if "Solving report" in output:
                                time.sleep(2)
                                process.kill()
                            elif output == "" and process.poll() is not None:
                                break
                            elif output:
                                print(output.strip())
                        except Exception:
                            print("File closed")
                            # traceback.print_exc()
                            break
                    process.kill()

                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                output_thread = threading.Thread(target=print_output, args=(process,))
                output_thread.start()
                output_thread.join()

            # Parsing
            with open(sol_file_name, "r") as f:
                for v in model.get_variables():
                    v.set_value(0)
                cols_started = False
                for line in f:
                    if not cols_started and "# Columns" not in line:
                        continue
                    elif "# Columns" in line:
                        cols_started = True
                        continue
                    elif cols_started and line[0] != "#":
                        words = line.split()
                        v = model.get_variable(words[0])
                        try:
                            if v.get_type() == so.INT:
                                v.set_value(round(float(words[1])))
                            elif v.get_type() == so.BIN:
                                v.set_value(round(float(words[1])))
                            elif v.get_type() == so.CONT:
                                v.set_value(round(float(words[1]), 3))
                        except Exception:
                            print("Error", words[0], line)
                    elif line[0] == "#":
                        break

        # DataFrame generation
        picks = []
        for w in gameweeks:
            for p in players:
                if squad[p, w].get_value() + squad_lr[p, w].get_value() + transfer_out[p, w].get_value() > 0.5:
                    lp = merged_data.loc[p]
                    is_captain = 1 if captain[p, w].get_value() > 0.5 else 0
                    is_squad = (
                        1
                        if (use_lr[w].get_value() < 0.5 and squad[p, w].get_value() > 0.5)
                        or (use_lr[w].get_value() > 0.5 and squad_lr[p, w].get_value() > 0.5)
                        else 0
                    )
                    is_lineup = 1 if lineup[p, w].get_value() > 0.5 else 0
                    is_vice = 1 if vicecap[p, w].get_value() > 0.5 else 0
                    is_transfer_in = 1 if transfer_in[p, w].get_value() > 0.5 else 0
                    is_transfer_out = 1 if transfer_out[p, w].get_value() > 0.5 else 0
                    bench_value = -1
                    for o in order:
                        if bench[p, w, o].get_value() > 0.5:
                            bench_value = o
                    position = type_data.loc[lp["element_type"], "singular_name_short"]
                    player_buy_price = 0 if not is_transfer_in else buy_price[p]
                    player_sell_price = (
                        0
                        if not is_transfer_out
                        else (sell_price[p] if p in price_modified_players and transfer_out_first[p, w].get_value() > 0.5 else buy_price[p])
                    )
                    multiplier = 1 * (is_lineup == 1) + 1 * (is_captain == 1)
                    xp_cont = points_player_week[p, w] * multiplier
                    current_iter = iter + 1

                    # chip
                    if use_wc[w].get_value() > 0.5:
                        chip_text = "WC"
                    elif use_lr[w].get_value() > 0.5:
                        chip_text = "LR"
                    elif use_dd[w].get_value() > 0.5:
                        chip_text = "DD"
                    elif use_ptb[w].get_value() > 0.5:
                        chip_text = "PTB"
                    else:
                        chip_text = ""

                    picks.append(
                        {
                            "id": p,
                            "week": w,
                            "name": lp["web_name"],
                            "pos": position,
                            "type": lp["element_type"],
                            "team": lp["name"],
                            "buy_price": player_buy_price,
                            "sell_price": player_sell_price,
                            "xP": round(points_player_week[p, w], 2),
                            "xMin": minutes_player_week[p, w],
                            "squad": is_squad,
                            "lineup": is_lineup,
                            "bench": bench_value,
                            "captain": is_captain,
                            "vicecaptain": is_vice,
                            "transfer_in": is_transfer_in,
                            "transfer_out": is_transfer_out,
                            "multiplier": multiplier,
                            "xp_cont": xp_cont,
                            "chip": chip_text,
                            "iter": current_iter,
                            "ft": free_transfers[w].get_value(),
                            "transfer_count": number_of_transfers[w].get_value(),
                        }
                    )

        picks_df = pd.DataFrame(picks).sort_values(by=["week", "lineup", "type", "xP"], ascending=[True, False, True, True])
        total_xp = so.expr_sum((lineup[p, w] + captain[p, w]) * points_player_week[p, w] for p in players for w in gameweeks).get_value()

        picks_df.sort_values(by=["week", "squad", "lineup", "bench", "type"], ascending=[True, False, False, True, True], inplace=True)

        # Writing summary
        summary_of_actions = ""
        move_summary = {"chip": [], "buy": [], "sell": []}
        cumulative_xpts = 0

        # collect statistics
        statistics = {}

        for w in gameweeks:
            summary_of_actions += f"** GW {w}:\n"
            chip_decision = (
                ("WC" if use_wc[w].get_value() > 0.5 else "")
                + ("LR" if use_lr[w].get_value() > 0.5 else "")
                + ("DD" if use_dd[w].get_value() > 0.5 else "")
                + ("PTB" if use_ptb[w].get_value() > 0.5 else "")
            )
            if chip_decision != "":
                summary_of_actions += "CHIP " + chip_decision + "\n"
                move_summary["chip"].append(chip_decision + str(w))
            summary_of_actions += f"ITB={in_the_bank[w - 1].get_value()}->{in_the_bank[w].get_value()}, FT={free_transfers[w].get_value()}, PT={penalized_transfers[w].get_value()}, NT={number_of_transfers[w].get_value()}\n"
            for p in players:
                if transfer_in[p, w].get_value() > 0.5:
                    summary_of_actions += f"Buy {p} - {merged_data['web_name'][p]}\n"
                    if w == next_gw:
                        move_summary["buy"].append(merged_data["web_name"][p])

            for p in players:
                if transfer_out[p, w].get_value() > 0.5:
                    summary_of_actions += f"Sell {p} - {merged_data['web_name'][p]}\n"
                    if w == next_gw:
                        move_summary["sell"].append(merged_data["web_name"][p])

            lineup_players = picks_df[(picks_df["week"] == w) & (picks_df["lineup"] == 1)]
            bench_players = picks_df[(picks_df["week"] == w) & (picks_df["bench"] >= 0)]
            summary_of_actions += "---\nLineup:\n"

            def get_display(row):
                return f"{row['name']} ({row['xP']}{', C' if row['captain'] == 1 else ''}{', V' if row['vicecaptain'] == 1 else ''})"

            for type in [1, 2, 3, 4]:
                type_players = lineup_players[lineup_players["type"] == type]
                entries = type_players.apply(get_display, axis=1)
                summary_of_actions += "\t" + ", ".join(entries.tolist()) + "\n"
            summary_of_actions += "Bench: \n\t" + ", ".join(bench_players["name"].tolist()) + "\n"
            summary_of_actions += "Lineup xPts: " + str(round(lineup_players["xp_cont"].sum(), 2)) + "\n"
            summary_of_actions += "---\n\n"
            cumulative_xpts = cumulative_xpts + round(lineup_players["xp_cont"].sum(), 2)
            statistics[w] = {
                "itb": in_the_bank[w].get_value(),
                "ft": free_transfers[w].get_value(),
                "pt": penalized_transfers[w].get_value(),
                "nt": number_of_transfers[w].get_value(),
                "xP": round(lineup_players["xp_cont"].sum(), 2),
                "obj": round(gw_total[w].get_value(), 2),
                "chip": chip_decision if chip_decision != "" else None,
            }

        print("Cumulative xPts: " + str(round(cumulative_xpts, 2)) + "\n---\n\n")

        if options.get("delete_tmp", True):
            time.sleep(0.1)
            try:
                try:
                    os.unlink(mps_file_name)
                except Exception:
                    pass
                try:
                    os.unlink(sol_file_name)
                except Exception:
                    pass
                try:
                    os.unlink(opt_file_name)
                except Exception:
                    pass
            except Exception:
                print("Could not delete temporary files")

        buy_decisions = ", ".join(move_summary["buy"])
        sell_decisions = ", ".join(move_summary["sell"])
        chip_decisions = ", ".join(move_summary["chip"])
        if buy_decisions == "":
            buy_decisions = "-"
        if sell_decisions == "":
            sell_decisions = "-"
        if chip_decisions == "":
            chip_decisions = "-"

        # Add current solution to a list, and add a new cut
        solutions.append(
            {
                "iter": iter,
                "model": model,
                "picks": picks_df,
                "total_xp": total_xp,
                "summary": summary_of_actions,
                "statistics": statistics,
                "buy": buy_decisions,
                "sell": sell_decisions,
                "chip": chip_decisions,
                "score": -model.get_objective_value(),
                "decay_metrics": {key: value.get_value() for key, value in decay_metrics.items()},
            }
        )

        if iteration == 1:
            return solutions

        iter_diff = options.get("iteration_difference", 1)

        if iteration_criteria == "this_gw_transfer_in":
            actions = so.expr_sum(1 - transfer_in[p, next_gw] for p in players if transfer_in[p, next_gw].get_value() > 0.5) + so.expr_sum(
                transfer_in[p, next_gw] for p in players if transfer_in[p, next_gw].get_value() < 0.5
            )
            model.add_constraint(actions >= 1, name=f"cutoff_{iter}")

        elif iteration_criteria == "this_gw_transfer_out":
            actions = so.expr_sum(1 - transfer_out[p, next_gw] for p in players if transfer_out[p, next_gw].get_value() > 0.5) + so.expr_sum(
                transfer_out[p, next_gw] for p in players if transfer_out[p, next_gw].get_value() < 0.5
            )
            model.add_constraint(actions >= 1, name=f"cutoff_{iter}")

        elif iteration_criteria == "this_gw_transfer_in_out":
            actions = (
                so.expr_sum(1 - transfer_in[p, next_gw] for p in players if transfer_in[p, next_gw].get_value() > 0.5)
                + so.expr_sum(transfer_in[p, next_gw] for p in players if transfer_in[p, next_gw].get_value() < 0.5)
                + so.expr_sum(1 - transfer_out[p, next_gw] for p in players if transfer_out[p, next_gw].get_value() > 0.5)
                + so.expr_sum(transfer_out[p, next_gw] for p in players if transfer_out[p, next_gw].get_value() < 0.5)
            )
            model.add_constraint(actions >= 1, name=f"cutoff_{iter}")

        elif iteration_criteria == "chip_gws":
            actions = (
                so.expr_sum(1 - use_wc[w] for w in gameweeks if use_wc[w].get_value() > 0.5)
                + so.expr_sum(use_wc[w] for w in gameweeks if use_wc[w].get_value() < 0.5)
                + so.expr_sum(1 - use_lr[w] for w in gameweeks if use_lr[w].get_value() > 0.5)
                + so.expr_sum(use_lr[w] for w in gameweeks if use_lr[w].get_value() < 0.5)
                + so.expr_sum(1 - use_dd[w] for w in gameweeks if use_dd[w].get_value() > 0.5)
                + so.expr_sum(use_dd[w] for w in gameweeks if use_dd[w].get_value() < 0.5)
                + so.expr_sum(1 - use_ptb[w] for w in gameweeks if use_ptb[w].get_value() > 0.5)
                + so.expr_sum(use_ptb[w] for w in gameweeks if use_ptb[w].get_value() < 0.5)
            )
            model.add_constraint(actions >= 1, name=f"cutoff_{iter}")

        elif iteration_criteria == "target_gws_transfer_in":
            target_gws = options.get("iteration_target", [next_gw])
            transferred_players = [[p, w] for p in players for w in target_gws if transfer_in[p, w].get_value() > 0.5]
            remaining_players = [[p, w] for p in players for w in target_gws if transfer_in[p, w].get_value() < 0.5]
            actions = so.expr_sum(1 - transfer_in[p, w] for [p, w] in transferred_players) + so.expr_sum(
                transfer_in[p, w] for [p, w] in remaining_players
            )
            model.add_constraint(actions >= 1, name=f"cutoff_{iter}")

        elif iteration_criteria == "this_gw_lineup":
            selected_lineup = [p for p in players if lineup[p, next_gw].get_value() > 0.5]
            model.add_constraint(so.expr_sum(lineup[p, next_gw] for p in selected_lineup) <= len(selected_lineup) - iter_diff, name=f"cutoff_{iter}")

        elif iteration_criteria == "chip_combinations":
            try:
                current_chips = chip_combinations[iter + 1]
            except Exception:
                break
            pairs = [
                {"chip": "wc", "variable": use_wc},
                {"chip": "lr", "variable": use_lr},
                {"chip": "dd", "variable": use_dd},
                {"chip": "ptb", "variable": use_ptb},
            ]
            for pair in pairs:
                chip = pair["chip"]
                variable = pair["variable"]
                model.drop_constraint(model.get_constraint(f"cc_{chip}"))
                model.drop_constraint(model.get_constraint(f"use_{chip}_limit"))

                if current_chips.get(chip) is not None:
                    model.add_constraint(variable[current_chips[chip]] == 1, name=f"cc_{chip}")
                    options["chip_limits"][chip] = 1
                    model.add_constraint(so.expr_sum(variable[w] for w in gameweeks) == 1, name=f"use_{chip}_limit")

                else:
                    model.add_constraint(so.expr_sum(variable[w] for w in gameweeks) == 0, name=f"cc_{chip}")
                    options["chip_limits"][chip] = 0
                    model.add_constraint(so.expr_sum(variable[w] for w in gameweeks) == 0, name=f"use_{chip}_limit")

    return solutions
