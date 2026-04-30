"""
Claim Engine v4 — Stateless Edition
Zasady w plikach JSON (config/). Zero bazy danych. Zero Turso.
"""

import streamlit as st
import pandas as pd
import json
import re
import io
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path

st.set_page_config(page_title="Claim Engine", page_icon="🎯", layout="wide")

CONFIG_DIR = Path(__file__).parent / "config"

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG LOADING
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_resource
def load_config():
    handlers      = json.loads((CONFIG_DIR / "handlers.json").read_text(encoding="utf-8"))
    rules_global  = json.loads((CONFIG_DIR / "rules_global.json").read_text(encoding="utf-8"))
    rules_nordic  = json.loads((CONFIG_DIR / "rules_nordic.json").read_text(encoding="utf-8"))
    schenker      = json.loads((CONFIG_DIR / "schenker.json").read_text(encoding="utf-8"))
    return handlers, rules_global, rules_nordic, schenker

# ═══════════════════════════════════════════════════════════════════════════════
# NORMALISATION
# ═══════════════════════════════════════════════════════════════════════════════

def norm_div(div: str) -> str:
    if not div:
        return ""
    d = str(div).strip()
    low = d.lower().replace(" ", "").replace("&", "")
    if "air" in low and "sea" in low:   return "A&S"
    if low in ("as", "airsea", "a&s"):  return "A&S"
    if "xpress" in low:                 return "XPress"
    if low == "cl":                     return "Contract Logistics"
    if "contract" in low and "log" in low: return "Contract Logistics"
    if "solution" in low:               return "Contract Logistics"
    if "road" in low:                   return "Road"
    return d

def norm_name(name: str) -> str:
    tbl = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszaAcelnOSZZ")
    r = str(name).translate(tbl)
    return re.sub(r"[\s\-\.\,\_]", "", r).lower()

def safe_float(v) -> float:
    try:
        f = float(v)
        return f if pd.notna(f) else 0.0
    except Exception:
        return 0.0

# ═══════════════════════════════════════════════════════════════════════════════
# ATTENDANCE (session-only)
# ═══════════════════════════════════════════════════════════════════════════════

def init_attendance(handlers):
    if "attendance" not in st.session_state:
        st.session_state["attendance"] = {h["name"]: True for h in handlers}

def present(name: str) -> bool:
    return st.session_state.get("attendance", {}).get(name, True)

# ═══════════════════════════════════════════════════════════════════════════════
# LOAD-BALANCED HANDLER PICKING
# ═══════════════════════════════════════════════════════════════════════════════

def pick(names: list, alt_names: list, counter: Counter, by_name: dict):
    """
    Round-robin among present primary handlers.
    Falls back to alt_names if all primary absent.
    Returns (name, rid, team) or (None, None, None).
    """
    for pool in (names, alt_names):
        available = [n for n in pool if present(n) and n in by_name]
        if available:
            chosen = min(available, key=lambda n: counter[n])
            counter[chosen] += 1
            h = by_name[chosen]
            return chosen, h["rid"], h["team"]
    return None, None, None

# ═══════════════════════════════════════════════════════════════════════════════
# SCHENKER CHECK
# ═══════════════════════════════════════════════════════════════════════════════

MERGE_YEAR = 2025

def check_schenker(shipment, country, division, dol, schenker_cfg):
    """Returns (team, rid, reason) tuple if Schenker claim, else None."""
    if not shipment or "-" in str(shipment):
        return None

    if dol is None or (hasattr(dol, "__class__") and dol.__class__.__name__ == "NaTType"):
        return ("Claims Schenker Legacy", "#N/A", "Schenker: brak Date of Loss")

    try:
        year = dol.year
    except Exception:
        return ("Claims Schenker Legacy", "#N/A", "Schenker: nieprawidłowa data")

    if year > MERGE_YEAR:
        return None
    if year < MERGE_YEAR:
        return ("Claims Schenker Legacy", "#N/A", f"Schenker Legacy: DoL < {MERGE_YEAR}")

    country_entries = [e for e in schenker_cfg if e["country"].lower() == country.lower()]
    if not country_entries:
        return ("Claims Schenker Legacy", "#N/A", f"Schenker Legacy: {country} nie w liście")

    for e in country_entries:
        div_match = e["division"] == "all" or norm_div(e["division"]) == division
        if div_match:
            if e.get("legacy"):
                return ("Claims Schenker Legacy", "#N/A", f"Schenker Legacy override: {country} {division}")
            return None  # scalony — przetwarzamy normalnie

    return ("Claims Schenker Legacy", "#N/A", f"Schenker Legacy: {country}/{division} poza listą")

# ═══════════════════════════════════════════════════════════════════════════════
# RULE MATCHING
# ═══════════════════════════════════════════════════════════════════════════════

def rule_matches(rule, country, division, sub_type, claimant, amount):
    if rule.get("countries"):
        if not any(country.lower() == c.lower() for c in rule["countries"]):
            return False
    if rule.get("divisions"):
        if division not in [norm_div(d) for d in rule["divisions"]]:
            return False
    if rule.get("sub_types"):
        if not any(s.lower() in sub_type.lower() for s in rule["sub_types"]):
            return False
    if rule.get("customer"):
        if norm_name(rule["customer"]) not in norm_name(claimant):
            return False
    if rule.get("min_amount") is not None and amount < rule["min_amount"]:
        return False
    if rule.get("max_amount") is not None and amount >= rule["max_amount"]:
        return False
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL ASSIGNMENT
# ═══════════════════════════════════════════════════════════════════════════════

def assign_global(row, cfg, schenker_cfg, counter, by_name):
    shipment  = str(row.get("Shipment number", "")).strip()
    country   = str(row.get("DSV Country (Lookup)", "")).strip()
    division  = norm_div(str(row.get("DSV Division (Lookup)", "")).strip())
    claimant  = str(row.get("Claimant Name", "")).strip()
    sub_type  = str(row.get("Claim Sub-Type", "")).strip()
    dol       = _parse_dol(row.get("Date of Loss"))
    amount    = _eff_amount(row)

    # 1 ─ Schenker
    sr = check_schenker(shipment, country, division, dol, schenker_cfg)
    if sr:
        return None, sr[0], sr[1], sr[2]

    # 2 ─ Special customers (Abbott, Adidas…)
    for sc in cfg["special_customers"]:
        if norm_name(sc["customer"]) in norm_name(claimant):
            name, rid, team = pick(sc["handlers"], sc.get("alt_handlers", []), counter, by_name)
            return (name, team or "CHC Global", rid or "", f"Special: {sc['customer']}")

    # 3 ─ XPress division
    if division == "XPress":
        name, rid, team = pick(cfg["xpress_handlers"], cfg.get("xpress_alt_handlers", []), counter, by_name)
        return (name, team or "CHC Global", rid or "", "XPress")

    # 4 ─ Low Value (amount > 0, < threshold, excluded sub-types bypass this)
    ft_excl = cfg["fast_track_exclusions"]
    sub_excluded = any(exc.lower() in sub_type.lower() for exc in ft_excl)

    if amount > 0 and amount < cfg["low_value_max"] and not sub_excluded:
        return (None, "CHC Low Value", "", f"Low Value: {amount:.0f} EUR")

    # 5 ─ Fast Track (200–500 EUR, excluded sub-types bypass this)
    if (amount >= cfg["fast_track_min"] and amount <= cfg["fast_track_max"] and not sub_excluded):
        return (None, "CHC Bucharest", "", f"Fast Track: {amount:.0f} EUR")

    # 6 ─ Standard rules
    for rule in cfg["rules"]:
        if not rule_matches(rule, country, division, sub_type, claimant, amount):
            continue
        name, rid, team = pick(rule["handlers"], rule.get("alt_handlers", []), counter, by_name)
        label = f"Rule: {rule['description']}"
        return (name, team or "CHC Global", rid or "", label)

    return (None, "", "#N/A", "Brak pasującej reguły")

# ═══════════════════════════════════════════════════════════════════════════════
# NORDIC ASSIGNMENT
# ═══════════════════════════════════════════════════════════════════════════════

def assign_nordic(row, cfg, schenker_cfg, counter, by_name):
    shipment  = str(row.get("Shipment number", "")).strip()
    country   = str(row.get("DSV Country (Lookup)", "")).strip()
    division  = norm_div(str(row.get("DSV Division (Lookup)", "")).strip())
    claimant  = str(row.get("Claimant Name", "")).strip()
    sub_type  = str(row.get("Claim Sub-Type", "")).strip()
    dol       = _parse_dol(row.get("Date of Loss"))
    amount    = _eff_amount(row)

    # 1 ─ Schenker
    sr = check_schenker(shipment, country, division, dol, schenker_cfg)
    if sr:
        return None, sr[0], sr[1], sr[2]

    # 2 ─ VIP customers
    for vip in cfg["vip_customers"]:
        if norm_name(vip["customer"]) not in norm_name(claimant):
            continue
        if vip.get("country") and vip["country"].lower() != country.lower():
            continue
        mn = vip.get("min_amount", 0)
        mx = vip.get("max_amount", 9999999)
        if not (mn <= amount < mx):
            continue
        # Team override (e.g. LEGO → CHC Global)
        if vip.get("output_team"):
            return (None, vip["output_team"], vip.get("output_rid", "#N/A"), f"VIP: {vip['customer']}")
        name, rid, team = pick(vip["handlers"], vip.get("alt_handlers", []), counter, by_name)
        return (name, team or "CHC Nordic", rid or "", f"VIP: {vip['customer']}")

    # 3 ─ Standard rules
    for rule in cfg["rules"]:
        if not rule_matches(rule, country, division, sub_type, claimant, amount):
            continue
        if rule.get("output_team"):
            name, rid, team = pick(rule["handlers"], rule.get("alt_handlers", []), counter, by_name)
            out_team = rule["output_team"] if not name else (team or rule["output_team"])
            return (name, out_team, rid or "", f"Rule: {rule['description']}")
        name, rid, team = pick(rule["handlers"], rule.get("alt_handlers", []), counter, by_name)
        return (name, team or "CHC Nordic", rid or "", f"Rule: {rule['description']}")

    return (None, "", "#N/A", "Brak pasującej reguły")

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_dol(v):
    if v is None or (hasattr(v, "__class__") and v.__class__.__name__ == "NaTType"):
        return None
    if isinstance(v, str):
        try:    return pd.to_datetime(v, dayfirst=True)
        except: return None
    if isinstance(v, (int, float)):
        try:    return pd.to_datetime("1899-12-30") + timedelta(days=float(v))
        except: return None
    return v  # already datetime

def _eff_amount(row) -> float:
    ca = safe_float(row.get("Claim amount EUR", 0))
    tl = safe_float(row.get("Total liability EUR", 0))
    if ca > 0 and tl > 0:
        return min(ca, tl)
    return max(ca, tl)

def build_output(row, handler_name, team_name, rid, reason):
    r = row.copy().astype(object)
    if "Claim: Claim Number" in r.index:
        r = r.rename({"Claim: Claim Number": "Claim Import ID"})

    dol = _parse_dol(row.get("Date of Loss"))
    if dol is not None:
        try:
            r["Date of Loss"] = dol.strftime("%d.%m.%Y")
            r["Timebar date liable party"] = (dol + timedelta(days=365)).strftime("%d.%m.%Y")
        except Exception:
            pass

    r["Assigned Name"]     = rid if rid else ""
    r["Claim Handler"]     = handler_name or ""
    r["Team Name"]         = team_name or ""
    r["Assignment Reason"] = reason
    r["Internal Status"]   = "Awaiting own process"
    r["Recovery Status"]   = "Awaiting own process"
    r["Initial assignment"]= datetime.now().strftime("%d.%m.%Y")

    if str(row.get("Status", "")).strip().lower() == "new":
        r["Status"] = "Assigned"
    return r

def reorder_columns(df):
    cols = list(df.columns)
    output_cols = ["Assigned Name", "Claim Handler", "Team Name", "Assignment Reason"]
    for c in output_cols:
        if c in cols: cols.remove(c)
    insert_at = cols.index("Claimant Name") + 1 if "Claimant Name" in cols else len(cols)
    for i, c in enumerate(output_cols):
        if c in df.columns:
            cols.insert(insert_at + i, c)
    for tail in ("Timebar date liable party",):
        if tail in cols:
            cols.remove(tail); cols.append(tail)
    return df[[c for c in cols if c in df.columns]]

def process_df(df, team, cfg, schenker_cfg, by_name):
    counter = Counter()
    results = []
    assign_fn = assign_global if team == "Global" else assign_nordic
    for _, row in df.iterrows():
        name, tname, rid, reason = assign_fn(row, cfg, schenker_cfg, counter, by_name)
        results.append(build_output(row, name, tname, rid, reason))
    result_df = reorder_columns(pd.DataFrame(results))
    return result_df, dict(counter)

# ═══════════════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    try:
        handlers, rules_global, rules_nordic, schenker = load_config()
    except Exception as e:
        st.error(f"❌ Błąd ładowania konfiguracji: {e}")
        st.stop()

    by_name = {h["name"]: h for h in handlers}
    init_attendance(handlers)

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.title("🎯 Claim Engine")
        st.caption("v4.0 — Stateless Edition")
        st.divider()

        team = st.radio("**Team**", ["Global", "Nordic"], horizontal=True,
                        key="team_select")

        st.divider()
        st.subheader("👥 Attendance")
        team_label = "CHC Nordic" if team == "Nordic" else "CHC Global"
        team_handlers = [h for h in handlers if h["team"] == team_label]

        cols_att = st.columns(2)
        for i, h in enumerate(team_handlers):
            short = h["name"].split()[0]
            current = st.session_state["attendance"].get(h["name"], True)
            new_val = cols_att[i % 2].toggle(short, value=current, key=f"att_{h['name']}")
            st.session_state["attendance"][h["name"]] = new_val

    cfg = rules_global if team == "Global" else rules_nordic

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab_process, tab_rules = st.tabs(["🚀 Process Claims", "📋 Zasady"])

    # ── PROCESS ──────────────────────────────────────────────────────────────
    with tab_process:
        st.header(f"🚀 Process Claims — CHC {team}")

        uploaded = st.file_uploader("Wgraj Excel (.xlsx)", type=["xlsx"], key="uploader")
        if uploaded:
            try:
                df = pd.read_excel(uploaded, engine="openpyxl")
            except Exception as e:
                st.error(f"Błąd odczytu pliku: {e}")
                st.stop()

            if "Date of Loss" in df.columns:
                df["Date of Loss"] = pd.to_datetime(
                    df["Date of Loss"], errors="coerce", dayfirst=True)

            st.info(f"Załadowano **{len(df)}** claimów")
            with st.expander("Podgląd (pierwsze 5 wierszy)"):
                st.dataframe(df.head(5), use_container_width=True)

            if st.button("🚀 **START PROCESSING**", type="primary",
                         use_container_width=True, key="btn_process"):
                with st.spinner("Przetwarzam…"):
                    result_df, stats = process_df(df, team, cfg, schenker, by_name)
                    st.session_state["result_df"] = result_df
                    st.session_state["stats"] = stats
                    st.session_state["result_team"] = team

        if "result_df" in st.session_state:
            result_df = st.session_state["result_df"]
            stats     = st.session_state.get("stats", {})
            res_team  = st.session_state.get("result_team", team)

            an = result_df.get("Assigned Name", pd.Series(dtype=str))
            assigned   = (an.notna() & (an != "") & (an != "#N/A")).sum()
            team_only  = (an == "").sum()
            unmatched  = (an == "#N/A").sum()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Łącznie",      len(result_df))
            c2.metric("Przypisane",   int(assigned))
            c3.metric("Tylko Team",   int(team_only),
                      help="Reguły kwotowe (Low Value / Fast Track) — Team przypisany, handler pusty")
            c4.metric("Nieprzypisane",int(unmatched))

            if stats:
                st.subheader("Rozkład przypisań")
                rows = sorted([{"Handler": n, "Claimy": c}
                                for n, c in stats.items()], key=lambda x: -x["Claimy"])
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

            st.subheader("Wyniki")
            preview = [c for c in [
                "Claim Import ID", "Claim: Claim Number",
                "DSV Country (Lookup)", "DSV Division (Lookup)",
                "Claim Sub-Type", "Claimant Name",
                "Claim amount EUR", "Total liability EUR",
                "Assigned Name", "Claim Handler", "Team Name", "Assignment Reason",
            ] if c in result_df.columns]
            st.dataframe(result_df[preview] if preview else result_df,
                         use_container_width=True, height=350)

            st.subheader("Pobierz")
            dc1, dc2 = st.columns(2)
            buf = io.BytesIO()
            result_df.to_excel(buf, index=False, engine="openpyxl")
            fname = f"Rozdanie {res_team} {datetime.now().strftime('%d.%m.%Y')}.xlsx"
            dc1.download_button("📥 Excel", buf.getvalue(), fname,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True, type="primary")
            dc2.download_button("📥 CSV",
                                result_df.to_csv(index=False).encode("utf-8"),
                                "claims_output.csv", "text/csv",
                                use_container_width=True)

    # ── RULES VIEWER ─────────────────────────────────────────────────────────
    with tab_rules:
        st.header(f"📋 Zasady — CHC {team}")
        st.caption("Zasady są w plikach `config/` na GitHub. "
                   "Aby je zmienić — wyślij nowy XLSX do administratora.")

        if team == "Global":
            # Fast Track info
            st.subheader("⚡ Reguły kwotowe (wszystkie kraje)")
            col_a, col_b = st.columns(2)
            col_a.info(
                f"**CHC Low Value**\n\n"
                f"Kwota > 0 i < **{cfg['low_value_max']} EUR**\n\n"
                f"Handler: *pusty*, team przypisany"
            )
            col_b.info(
                f"**CHC Bucharest — Fast Track**\n\n"
                f"Kwota **{cfg['fast_track_min']}–{cfg['fast_track_max']} EUR**\n\n"
                f"Handler: *pusty*, team przypisany"
            )
            st.warning(
                "**Wyjątki** (te typy szkód idą do standardowych reguł, ignorując kwotę): "
                + ", ".join(cfg["fast_track_exclusions"])
            )

            # Special customers
            st.subheader("⭐ Special Customers")
            sc_rows = [{"Customer": sc["customer"],
                        "Handlers (primary)": ", ".join(sc["handlers"]),
                        "Alt handlers": ", ".join(sc.get("alt_handlers", []))}
                       for sc in cfg["special_customers"]]
            st.dataframe(pd.DataFrame(sc_rows), hide_index=True, use_container_width=True)

            # XPress
            st.subheader("📦 XPress")
            st.info(f"Handlers: **{', '.join(cfg['xpress_handlers'])}**")

        else:  # Nordic
            st.subheader("⭐ VIP Customers")
            vip_rows = [{"Customer": v["customer"],
                         "Country": v.get("country") or "Wszystkie",
                         "Min EUR": v.get("min_amount", ""),
                         "Max EUR": "" if v.get("max_amount", 0) >= 9999999 else v.get("max_amount"),
                         "Handlers": ", ".join(v.get("handlers", [])) or f"→ {v.get('output_team','')}",
                         "Alt": ", ".join(v.get("alt_handlers", []))}
                        for v in cfg["vip_customers"]]
            st.dataframe(pd.DataFrame(vip_rows), hide_index=True, use_container_width=True)

        # Standard rules
        st.subheader("📋 Reguły standardowe")
        rule_rows = []
        for r in cfg["rules"]:
            rule_rows.append({
                "Opis":       r["description"],
                "Kraje":      ", ".join(r.get("countries", [])) or "Wszystkie",
                "Dywizje":    ", ".join(r.get("divisions", [])) or "Wszystkie",
                "Sub-typy":   ", ".join(r.get("sub_types", [])) or "",
                "Min EUR":    r.get("min_amount", ""),
                "Max EUR":    r.get("max_amount", ""),
                "Handlers":   ", ".join(r.get("handlers", [])),
                "Alternative":   ", ".join(r.get("alt_handlers", [])),
            })
        st.dataframe(pd.DataFrame(rule_rows), hide_index=True,
                     use_container_width=True, height=400)

        # All handlers
        st.subheader("👥 Wszyscy handlerzy")
        h_rows = [{"Imię i nazwisko": h["name"],
                   "Riskonnect ID":   h["rid"],
                   "Team":            h["team"]}
                  for h in handlers]
        st.dataframe(pd.DataFrame(h_rows), hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
