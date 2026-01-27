# app.py
#
# FAOA Annual Consolidation App (Updated Schema: supports both 1023/990-EZ + Form 990 tagging)
#
# What this app does:
# - Upload 1–12 monthly files (CSV or Excel) produced by the FAOA Monthly Treasurer Tool
# - Validates schema against the authoritative monthly export format
# - Robustly de-duplicates transactions across uploads
# - Generates TWO annual text reports:
#   (1) Annual 1023 / 990-EZ style report (FAOA categories + itemization)
#   (2) "Smart" Form 990 worksheet-style report (totals by 990 line + functional categories + itemization)
# - Optionally downloads a merged annual CSV for recordkeeping
#
# Notes:
# - The app does NOT store any data permanently.
# - It uses the labels and itemization columns from the monthly exports exactly as provided.

import hashlib
import io
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Basic page config
# ---------------------------------------------------------------------------

st.set_page_config(page_title="FAOA Annual Consolidation (1023 + 990)", layout="wide")

# ---------------------------------------------------------------------------
# Password protection
# ---------------------------------------------------------------------------


def check_password():
    """
    Simple password gate using Streamlit secrets.

    On Streamlit Cloud, set:
      APP_PASSWORD = "your-password-here"
    in the app's Secrets.
    """
    secret_key = "APP_PASSWORD"

    if secret_key not in st.secrets:
        st.error(
            f"Missing `{secret_key}` in Streamlit secrets. "
            "Set it in the app settings on Streamlit Cloud."
        )
        st.stop()

    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if not st.session_state["password_correct"]:
        st.title("FAOA Annual Consolidation (1023 + 990)")
        st.write("This tool is password protected.")
        password = st.text_input("Enter password", type="password")
        if password == "":
            st.stop()

        if password == st.secrets[secret_key]:
            st.session_state["password_correct"] = True
        else:
            st.error("Incorrect password.")
            st.stop()


check_password()

# ---------------------------------------------------------------------------
# Title / Description
# ---------------------------------------------------------------------------

st.title("FAOA Annual Consolidation (1023 + 990)")
st.write(
    "Upload **1–12** monthly files produced by the FAOA Monthly Treasurer Tool (CSV or Excel). "
    "This app consolidates them into annual outputs and generates two separate tax-ready text reports:\n\n"
    "1) **Annual 1023 / 990-EZ style report** (FAOA IRS categories + itemization)\n"
    "2) **Annual Form 990 worksheet report** (totals by 990 line + functional categories + itemization)\n\n"
    "No data is stored permanently."
)

# ---------------------------------------------------------------------------
# Authoritative schema (derived from your provided monthly export example)
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = [
    "Year",
    "Month",
    "Date",
    "Description",
    "Amount",
    "Itemization Preset",
    "Itemization Label (Common)",
    "Sponsor Name",
    "Member/Event Label",
    "Event Location",
    "Event Purpose",
    "Potential Sponsorship",
    "Needs Further Investigation",
    "IRS Category (1023)",
    "IRS Category Code (1023)",
    "IRS Category Label (1023)",
    "Itemization Label (1023)",
    "Form 990 Revenue Line",
    "Form 990 Expense Line",
    "Form 990 Functional Category",  # Added: Program, M&G, or Fundraising
    "Itemization Label (990)",
]

# For the 1023 report: which categories count as revenue/expense
# (Category codes appear as numbers; we store as strings)
REVENUE_CODES_1023 = {"1", "2", "3", "4", "6", "7", "9"}
EXPENSE_CODES_1023 = {"14", "15", "16", "18", "19", "22", "23"}

# Explanation text for professional fees (Category 22)
PROFESSIONAL_FEES_EXPLANATION = (
    "Professional fees include accounting, legal, and other professional services "
    "contracted by FAOA to support organizational operations and compliance."
)

# Valid functional categories for Form 990 Part IX
VALID_FUNCTIONAL_CATEGORIES = {"Program", "M&G", "Fundraising"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_str(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip()


def _coerce_bool(s: pd.Series) -> pd.Series:
    # Handles True/False, 1/0, yes/no, y/n, etc.
    return s.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def format_currency(x: float) -> str:
    if pd.isna(x):
        return "$0.00"
    return f"${x:,.2f}"


def read_upload(file) -> pd.DataFrame:
    name = (file.name or "").lower()
    try:
        if name.endswith(".xlsx") or name.endswith(".xls"):
            return pd.read_excel(file)
        return pd.read_csv(file)
    except Exception as e:
        st.error(f"Error reading '{file.name}': {e}")
        st.stop()


def validate_schema(df: pd.DataFrame, filename: str):
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        st.error(
            f"File '{filename}' is missing required column(s): {', '.join(missing)}.\n\n"
            "This annual app expects the exact monthly export schema."
        )
        st.stop()


def normalize_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # numeric
    df["Year"] = pd.to_numeric(df["Year"], errors="coerce")
    df["Month"] = pd.to_numeric(df["Month"], errors="coerce")
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")

    if df[["Year", "Month", "Amount"]].isna().any().any():
        st.error("Invalid numeric values found in Year, Month, or Amount.")
        st.stop()

    # strings
    str_cols = [
        "Date",
        "Description",
        "Itemization Preset",
        "Itemization Label (Common)",
        "Sponsor Name",
        "Member/Event Label",
        "Event Location",
        "Event Purpose",
        "IRS Category (1023)",
        "IRS Category Code (1023)",
        "IRS Category Label (1023)",
        "Itemization Label (1023)",
        "Form 990 Revenue Line",
        "Form 990 Expense Line",
        "Form 990 Functional Category",
        "Itemization Label (990)",
    ]
    for c in str_cols:
        df[c] = _clean_str(df[c])

    # booleans
    df["Potential Sponsorship"] = _coerce_bool(df["Potential Sponsorship"])
    df["Needs Further Investigation"] = _coerce_bool(df["Needs Further Investigation"])

    # normalize code to string
    df["IRS Category Code (1023)"] = _clean_str(df["IRS Category Code (1023)"])

    return df


def build_transaction_fingerprint(row: pd.Series) -> str:
    """
    Robust transaction de-dupe key.

    We intentionally use a stable subset that should be identical for the same transaction:
    Year, Month, Date, Description, Amount.

    - If you later add a RowID column in the monthly export, you can incorporate it here.
    """
    parts = [
        str(int(row["Year"])) if pd.notna(row["Year"]) else "",
        str(int(row["Month"])) if pd.notna(row["Month"]) else "",
        row.get("Date", ""),
        row.get("Description", ""),
        # Normalize amount to cents to avoid floating drift
        f"{float(row.get('Amount', 0.0)):.2f}",
    ]
    raw = "||".join(parts).strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def dedupe_transactions(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    df = df.copy()
    df["_tx_fingerprint"] = df.apply(build_transaction_fingerprint, axis=1)
    before = len(df)
    df = df.drop_duplicates(subset=["_tx_fingerprint"]).drop(columns=["_tx_fingerprint"])
    removed = before - len(df)
    return df, removed


def validate_single_year(df: pd.DataFrame) -> int:
    years = sorted(df["Year"].dropna().unique())
    if len(years) != 1:
        st.error(f"Uploaded files must all be for a single year. Found years: {years}")
        st.stop()
    return int(years[0])


def month_coverage(df: pd.DataFrame) -> Tuple[List[int], List[int]]:
    months = sorted(set(int(m) for m in df["Month"].dropna().tolist() if 1 <= int(m) <= 12))
    missing = [m for m in range(1, 13) if m not in months]
    return months, missing


def label_for_itemization_1023(df: pd.DataFrame) -> pd.Series:
    """
    For 1023 itemization: prefer 'Itemization Label (1023)' if present; else fallback to common label.
    Never blank in reports: use 'Not itemized'.
    """
    lbl = df["Itemization Label (1023)"].copy()
    lbl = lbl.where(lbl.str.strip() != "", df["Itemization Label (Common)"])
    lbl = lbl.where(lbl.str.strip() != "", "Not itemized")
    return lbl


def label_for_itemization_990(df: pd.DataFrame) -> pd.Series:
    """
    For 990 itemization: prefer 'Itemization Label (990)' if present; else fallback to common label.
    Never blank in reports: use 'Not itemized'.
    """
    lbl = df["Itemization Label (990)"].copy()
    lbl = lbl.where(lbl.str.strip() != "", df["Itemization Label (Common)"])
    lbl = lbl.where(lbl.str.strip() != "", "Not itemized")
    return lbl


# ---------------------------------------------------------------------------
# Report 1: Annual 1023 / 990-EZ style report
# ---------------------------------------------------------------------------


def build_report_1023(year: int, df: pd.DataFrame, gala_adjustment: float = 0.0) -> str:
    lines: List[str] = []

    lines.append(f"{year} Foreign Area Officer Association Annual Financial Report (1023 / 990-EZ Style)")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("=" * 70)
    lines.append("")

    # Note gala adjustment if applicable
    if gala_adjustment > 0:
        lines.append("NOTE: Gala Revenue Adjustment Applied")
        lines.append("-" * 40)
        lines.append(f"  ${gala_adjustment:,.2f} reallocated from Category 2 (Membership fees)")
        lines.append(f"  to Category 6 (Fundraising events) to correct Stripe commingling.")
        lines.append("")

    # Summary totals by 1023 category
    df = df.copy()
    df["__code"] = df["IRS Category Code (1023)"].astype(str)

    # Calculate raw totals
    raw_revenue = df[df["__code"].isin(REVENUE_CODES_1023)]["Amount"].sum()
    total_expenses = df[df["__code"].isin(EXPENSE_CODES_1023)]["Amount"].abs().sum()
    
    # Gala adjustment doesn't change total revenue, just reallocation
    total_revenue = raw_revenue
    net_change = total_revenue - total_expenses

    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Total Revenue:  {format_currency(total_revenue)}")
    lines.append(f"  Total Expenses: {format_currency(total_expenses)}")
    lines.append(f"  Net Change:     {format_currency(net_change)}")
    lines.append("")

    # Revenue summary with gala adjustment
    lines.append("REVENUE CATEGORIES (by IRS Category Code 1023)")
    lines.append("-" * 40)
    rev = df[df["__code"].isin(REVENUE_CODES_1023)]
    if rev.empty:
        lines.append("  (No revenue recorded for this period.)")
    else:
        rev_summary = (
            rev.groupby(["IRS Category Code (1023)", "IRS Category Label (1023)"])["Amount"]
            .sum()
            .reset_index()
        )
        rev_summary["__sort"] = pd.to_numeric(rev_summary["IRS Category Code (1023)"], errors="coerce")
        rev_summary = rev_summary.sort_values("__sort").drop(columns="__sort")
        
        # Track if we need to add Category 6
        has_cat_6 = "6" in rev_summary["IRS Category Code (1023)"].values
        
        for _, r in rev_summary.iterrows():
            code = r["IRS Category Code (1023)"]
            amount = r["Amount"]
            label = r["IRS Category Label (1023)"]
            
            # Apply gala adjustment
            if code == "2" and gala_adjustment > 0:
                adjusted_amount = amount - gala_adjustment
                lines.append(
                    f"  {code} - {label}: {format_currency(adjusted_amount)} "
                    f"(adjusted from {format_currency(amount)})"
                )
            elif code == "6" and gala_adjustment > 0:
                adjusted_amount = amount + gala_adjustment
                lines.append(
                    f"  {code} - {label}: {format_currency(adjusted_amount)} "
                    f"(adjusted from {format_currency(amount)})"
                )
            else:
                lines.append(f"  {code} - {label}: {format_currency(amount)}")
        
        # If gala adjustment exists but no Category 6 in data, add it
        if gala_adjustment > 0 and not has_cat_6:
            lines.append(
                f"  6 - Gross receipts from fundraising events: {format_currency(gala_adjustment)} "
                f"(added via Gala adjustment)"
            )

    lines.append("")
    lines.append("EXPENSE CATEGORIES (by IRS Category Code 1023)")
    lines.append("-" * 40)
    exp = df[df["__code"].isin(EXPENSE_CODES_1023)]
    if exp.empty:
        lines.append("  (No expenses recorded for this period.)")
    else:
        exp_summary = (
            exp.groupby(["IRS Category Code (1023)", "IRS Category Label (1023)"])["Amount"]
            .sum()
            .reset_index()
        )
        exp_summary["__sort"] = pd.to_numeric(exp_summary["IRS Category Code (1023)"], errors="coerce")
        exp_summary = exp_summary.sort_values("__sort").drop(columns="__sort")
        for _, r in exp_summary.iterrows():
            # Show expenses as positive values for clarity
            lines.append(
                f"  {r['IRS Category Code (1023)']} - {r['IRS Category Label (1023)']}: {format_currency(abs(r['Amount']))}"
            )

    # Itemized revenue
    lines.append("")
    lines.append("=" * 70)
    lines.append("ITEMIZED REVENUE (by 1023 category + itemization label)")
    lines.append("=" * 70)
    lines.append("")

    if rev.empty:
        lines.append("  (No itemized revenue entries.)")
    else:
        rev = rev.copy()
        rev["Itemization_Label_For_Report"] = label_for_itemization_1023(rev)

        # Sponsorship / donor totals by Sponsor Name (where Sponsor Name present)
        sponsor = rev[rev["Sponsor Name"].str.strip() != ""]
        if not sponsor.empty:
            lines.append("SPONSORSHIP / DONOR TOTALS (by Sponsor Name)")
            lines.append("-" * 40)
            sponsor_group = (
                sponsor.groupby("Sponsor Name")["Amount"].sum().reset_index().sort_values("Sponsor Name")
            )
            for _, r in sponsor_group.iterrows():
                lines.append(f"  {r['Sponsor Name']}: {format_currency(r['Amount'])}")
            lines.append("")

        # Itemize revenue by category then itemization label
        for code in sorted(REVENUE_CODES_1023, key=lambda x: int(x)):
            cat = rev[rev["IRS Category Code (1023)"] == code].copy()
            if cat.empty:
                continue

            label = cat["IRS Category Label (1023)"].iloc[0]
            cat_total = cat["Amount"].sum()
            lines.append(f"Category {code} – {label}: {format_currency(cat_total)}")
            lines.append("-" * 40)

            group = (
                cat.groupby("Itemization_Label_For_Report")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization_Label_For_Report")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization_Label_For_Report']}: {format_currency(r['Amount'])}")
            lines.append("")

    # Itemized expenses
    lines.append("")
    lines.append("=" * 70)
    lines.append("ITEMIZED EXPENSES (by 1023 category + itemization label)")
    lines.append("=" * 70)
    lines.append("")

    if exp.empty:
        lines.append("  (No itemized expense entries.)")
    else:
        exp = exp.copy()
        exp["Itemization_Label_For_Report"] = label_for_itemization_1023(exp)

        # Category 16: event detail listing (if present)
        cat16 = exp[exp["IRS Category Code (1023)"] == "16"].copy()
        if not cat16.empty and (
            cat16["Member/Event Label"].str.strip().ne("").any()
            or cat16["Event Location"].str.strip().ne("").any()
            or cat16["Event Purpose"].str.strip().ne("").any()
        ):
            lines.append("CATEGORY 16 – EVENTS (line-item detail)")
            lines.append("-" * 70)
            lines.append("  Date | Event | Location | Purpose | Amount | Description")
            lines.append("  " + "-" * 66)
            cat16 = cat16.sort_values(["Date", "Member/Event Label", "Description"])
            for _, r in cat16.iterrows():
                lines.append(
                    f"  {r['Date']} | {r['Member/Event Label']} | {r['Event Location']} | {r['Event Purpose']} | "
                    f"{format_currency(abs(r['Amount']))} | {r['Description']}"
                )
            lines.append("")

        # Itemize expenses by category
        for code in sorted(EXPENSE_CODES_1023, key=lambda x: int(x)):
            cat = exp[exp["IRS Category Code (1023)"] == code].copy()
            if cat.empty:
                continue

            label = cat["IRS Category Label (1023)"].iloc[0]
            cat_total = cat["Amount"].abs().sum()
            lines.append(f"Category {code} – {label}: {format_currency(cat_total)}")
            lines.append("-" * 40)

            # Add professional fees explanation for Category 22
            if code == "22":
                lines.append(f"    Note: {PROFESSIONAL_FEES_EXPLANATION}")
                lines.append("")

            group = (
                cat.groupby("Itemization_Label_For_Report")["Amount"]
                .apply(lambda x: abs(x).sum())
                .reset_index()
                .sort_values("Itemization_Label_For_Report")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization_Label_For_Report']}: {format_currency(r['Amount'])}")
            lines.append("")

    # Needs Further Investigation
    lines.append("")
    lines.append("=" * 70)
    lines.append("NEEDS FURTHER INVESTIGATION")
    lines.append("=" * 70)
    flagged = df[df["Needs Further Investigation"] == True].copy()
    if flagged.empty:
        lines.append("  (None flagged this period.)")
    else:
        lines.append(f"  Count of flagged transactions: {len(flagged)}")
        lines.append(f"  Net total of flagged amounts: {format_currency(flagged['Amount'].sum())}")
        lines.append("")
        # Optional detail list (kept readable, not a full dump)
        flagged = flagged.sort_values(["Month", "Date", "Description"])
        lines.append("  Flagged line items (up to first 50):")
        lines.append("  " + "-" * 66)
        show = flagged.head(50)
        for _, r in show.iterrows():
            lines.append(
                f"    {int(r['Month']):02d}/{int(r['Year'])} | {r['Date']} | {format_currency(r['Amount'])} | "
                f"{r['IRS Category Code (1023)']} | {r['Description']}"
            )
        if len(flagged) > 50:
            lines.append("    ... (more flagged items not shown)")

    lines.append("")
    lines.append("=" * 70)
    lines.append("End of 1023 / 990-EZ Annual Report")
    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report 2: Smart annual report for Form 990 preparation
# ---------------------------------------------------------------------------


def build_report_990(year: int, df: pd.DataFrame, gala_adjustment: float = 0.0) -> str:
    lines: List[str] = []

    lines.append(f"{year} FAOA Form 990 Worksheet (Annual Rollup)")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("EIN: [Enter EIN]")
    lines.append("=" * 80)
    lines.append("")
    lines.append("PURPOSE: This worksheet provides line-by-line totals for Form 990 data entry.")
    lines.append("All amounts are shown as POSITIVE values for direct entry into tax software.")
    lines.append("")

    # Note gala adjustment if applicable
    if gala_adjustment > 0:
        lines.append("NOTE: Gala Revenue Adjustment Applied")
        lines.append("-" * 50)
        lines.append(f"  ${gala_adjustment:,.2f} reallocated from Line 1b (Membership dues)")
        lines.append(f"  to Line 8a (Fundraising event revenue) to correct Stripe commingling.")
        lines.append("")

    df = df.copy()
    df["Itemization_Label_For_990"] = label_for_itemization_990(df)

    deposits = df[df["Amount"] > 0].copy()
    withdrawals = df[df["Amount"] < 0].copy()

    # Calculate grand totals (gala adjustment doesn't change total, just reallocation)
    total_revenue = deposits["Amount"].sum() if not deposits.empty else 0
    total_expenses = withdrawals["Amount"].abs().sum() if not withdrawals.empty else 0
    net_change = total_revenue - total_expenses

    # =========================================================================
    # QUICK REFERENCE: KEY FORM 990 TOTALS
    # =========================================================================
    lines.append("=" * 80)
    lines.append("QUICK REFERENCE: KEY FORM 990 LINE ENTRIES")
    lines.append("=" * 80)
    lines.append("")
    lines.append("Copy these totals directly into Form 990:")
    lines.append("")
    lines.append("PART I - SUMMARY")
    lines.append("-" * 50)
    lines.append(f"  Line 8   Total revenue (Part VIII, line 12):     {format_currency(total_revenue)}")
    lines.append(f"  Line 18  Total expenses (Part IX, line 25):      {format_currency(total_expenses)}")
    lines.append(f"  Line 19  Revenue less expenses:                  {format_currency(net_change)}")
    lines.append("")

    # Compute functional totals for quick reference
    if not withdrawals.empty:
        withdrawals["AbsAmount"] = withdrawals["Amount"].abs()
        withdrawals["__func_cat"] = _clean_str(withdrawals["Form 990 Functional Category"])
        
        prog_total = withdrawals[withdrawals["__func_cat"] == "Program"]["AbsAmount"].sum()
        mg_total = withdrawals[withdrawals["__func_cat"] == "M&G"]["AbsAmount"].sum()
        fund_total = withdrawals[withdrawals["__func_cat"] == "Fundraising"]["AbsAmount"].sum()
        unallocated = withdrawals[~withdrawals["__func_cat"].isin(VALID_FUNCTIONAL_CATEGORIES)]["AbsAmount"].sum()
    else:
        prog_total = mg_total = fund_total = unallocated = 0

    lines.append("PART IX - FUNCTIONAL EXPENSE COLUMN TOTALS (Line 25)")
    lines.append("-" * 50)
    lines.append(f"  Column (A) Total expenses:        {format_currency(total_expenses)}")
    lines.append(f"  Column (B) Program services:      {format_currency(prog_total)}")
    lines.append(f"  Column (C) Management & General:  {format_currency(mg_total)}")
    lines.append(f"  Column (D) Fundraising:           {format_currency(fund_total)}")
    if unallocated > 0:
        lines.append(f"  ⚠️  UNALLOCATED (needs category):  {format_currency(unallocated)}")
    lines.append("")

    # Schedule B check
    if not deposits.empty:
        sponsor = deposits[deposits["Sponsor Name"].str.strip() != ""]
        if not sponsor.empty:
            sponsor_totals = sponsor.groupby("Sponsor Name")["Amount"].sum()
            large_donors = sponsor_totals[sponsor_totals >= 5000]
            if not large_donors.empty:
                lines.append("⚠️  SCHEDULE B REQUIRED: Donors with contributions ≥$5,000")
                lines.append("-" * 50)
                for name, amt in large_donors.items():
                    lines.append(f"  {name}: {format_currency(amt)}")
                lines.append("")

    # =========================================================================
    # PART VIII: STATEMENT OF REVENUE (DETAILED)
    # =========================================================================
    lines.append("=" * 80)
    lines.append("PART VIII: STATEMENT OF REVENUE (Detailed)")
    lines.append("=" * 80)
    lines.append("")

    if deposits.empty:
        lines.append("  (No revenue recorded for this period.)")
        lines.append("")
    else:
        deposits["Form 990 Revenue Line"] = _clean_str(deposits["Form 990 Revenue Line"])
        deposits["__rev_line"] = deposits["Form 990 Revenue Line"].where(
            deposits["Form 990 Revenue Line"].str.strip() != "",
            "⚠️ MISSING LINE TAG",
        )

        # Group and sort by line number
        rev_rollup = (
            deposits.groupby("__rev_line")["Amount"].sum().reset_index().sort_values("__rev_line")
        )
        
        # Track if we need to add Line 8a
        has_line_8a = any("8a" in str(line) for line in rev_rollup["__rev_line"].values)
        
        for _, r in rev_rollup.iterrows():
            rev_line = r["__rev_line"]
            total = r["Amount"]
            
            # Apply gala adjustment to membership dues (Line 1b)
            if "1b" in rev_line and gala_adjustment > 0:
                adjusted_total = total - gala_adjustment
                if rev_line.startswith("⚠️"):
                    lines.append(f"{rev_line}: {format_currency(adjusted_total)} (adjusted from {format_currency(total)})")
                else:
                    lines.append(f"Line {rev_line}: {format_currency(adjusted_total)} (adjusted from {format_currency(total)})")
            # Apply gala adjustment to fundraising revenue (Line 8a) if it exists
            elif "8a" in rev_line and gala_adjustment > 0:
                adjusted_total = total + gala_adjustment
                if rev_line.startswith("⚠️"):
                    lines.append(f"{rev_line}: {format_currency(adjusted_total)} (adjusted from {format_currency(total)})")
                else:
                    lines.append(f"Line {rev_line}: {format_currency(adjusted_total)} (adjusted from {format_currency(total)})")
            else:
                if rev_line.startswith("⚠️"):
                    lines.append(f"{rev_line}: {format_currency(total)}")
                else:
                    lines.append(f"Line {rev_line}: {format_currency(total)}")

            # Itemization detail
            sub = deposits[deposits["__rev_line"] == rev_line].copy()
            sub_group = (
                sub.groupby("Itemization_Label_For_990")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization_Label_For_990")
            )
            for _, rr in sub_group.iterrows():
                lines.append(f"      • {rr['Itemization_Label_For_990']}: {format_currency(rr['Amount'])}")
            
            # Add gala ticket itemization note if this is Line 1b and adjustment applies
            if "1b" in rev_line and gala_adjustment > 0:
                lines.append(f"      • [Gala tickets moved to Line 8a]: -${gala_adjustment:,.2f}")
            
            lines.append("")
        
        # If gala adjustment exists but no Line 8a in data, add it
        if gala_adjustment > 0 and not has_line_8a:
            lines.append(f"Line 8a - Gross receipts from fundraising events: {format_currency(gala_adjustment)} (added via Gala adjustment)")
            lines.append(f"      • Gala ticket sales (via Stripe): {format_currency(gala_adjustment)}")
            lines.append("")

        # Donor/sponsor detail for Schedule B preparation
        sponsor = deposits[deposits["Sponsor Name"].str.strip() != ""]
        if not sponsor.empty:
            lines.append("-" * 50)
            lines.append("CONTRIBUTOR DETAIL (for Schedule B preparation)")
            lines.append("-" * 50)
            sgroup = sponsor.groupby("Sponsor Name")["Amount"].sum().reset_index().sort_values("Amount", ascending=False)
            for _, sr in sgroup.iterrows():
                flag = " ⬅ Schedule B" if sr["Amount"] >= 5000 else ""
                lines.append(f"  {sr['Sponsor Name']}: {format_currency(sr['Amount'])}{flag}")
            lines.append("")

        # Missing tags warning
        missing_rev = deposits[deposits["Form 990 Revenue Line"].str.strip() == ""].copy()
        if not missing_rev.empty:
            lines.append("⚠️  ACTION REQUIRED: MISSING REVENUE LINE TAGS")
            lines.append("-" * 50)
            lines.append(f"  Count: {len(missing_rev)} transactions")
            lines.append(f"  Total: {format_currency(missing_rev['Amount'].sum())}")
            lines.append("  Transactions needing correction:")
            show = missing_rev.sort_values(["Month", "Date"]).head(25)
            for _, mr in show.iterrows():
                lines.append(f"    {int(mr['Month']):02d}/{mr['Date']} | {format_currency(mr['Amount'])} | {mr['Description'][:50]}")
            if len(missing_rev) > 25:
                lines.append(f"    ... and {len(missing_rev) - 25} more")
            lines.append("")

    # =========================================================================
    # PART IX: STATEMENT OF FUNCTIONAL EXPENSES (DETAILED)
    # =========================================================================
    lines.append("=" * 80)
    lines.append("PART IX: STATEMENT OF FUNCTIONAL EXPENSES (Detailed)")
    lines.append("=" * 80)
    lines.append("")
    lines.append("Each line shows: Total (A) | Program (B) | M&G (C) | Fundraising (D)")
    lines.append("")

    if withdrawals.empty:
        lines.append("  (No expenses recorded for this period.)")
    else:
        withdrawals["Form 990 Expense Line"] = _clean_str(withdrawals["Form 990 Expense Line"])
        withdrawals["__exp_line"] = withdrawals["Form 990 Expense Line"].where(
            withdrawals["Form 990 Expense Line"].str.strip() != "",
            "⚠️ MISSING LINE TAG",
        )
        
        withdrawals["__func_cat_display"] = withdrawals["__func_cat"].where(
            withdrawals["__func_cat"].isin(VALID_FUNCTIONAL_CATEGORIES),
            "UNALLOCATED"
        )

        # Process each expense line
        for exp_line in sorted(withdrawals["__exp_line"].unique()):
            exp_subset = withdrawals[withdrawals["__exp_line"] == exp_line]
            
            line_total = exp_subset["AbsAmount"].sum()
            prog_amt = exp_subset[exp_subset["__func_cat"] == "Program"]["AbsAmount"].sum()
            mg_amt = exp_subset[exp_subset["__func_cat"] == "M&G"]["AbsAmount"].sum()
            fund_amt = exp_subset[exp_subset["__func_cat"] == "Fundraising"]["AbsAmount"].sum()
            unalloc_amt = exp_subset[~exp_subset["__func_cat"].isin(VALID_FUNCTIONAL_CATEGORIES)]["AbsAmount"].sum()

            # Line header
            if exp_line.startswith("⚠️"):
                lines.append(f"{exp_line}")
            else:
                lines.append(f"Line {exp_line}")
            
            lines.append(f"  (A) Total:       {format_currency(line_total)}")
            lines.append(f"  (B) Program:     {format_currency(prog_amt)}")
            lines.append(f"  (C) M&G:         {format_currency(mg_amt)}")
            lines.append(f"  (D) Fundraising: {format_currency(fund_amt)}")
            if unalloc_amt > 0:
                lines.append(f"  ⚠️  Unallocated:  {format_currency(unalloc_amt)}")

            # Itemization
            lines.append("  Itemization:")
            sub_group = (
                exp_subset.groupby("Itemization_Label_For_990")["AbsAmount"]
                .sum()
                .reset_index()
                .sort_values("AbsAmount", ascending=False)
            )
            for _, rr in sub_group.iterrows():
                lines.append(f"      • {rr['Itemization_Label_For_990']}: {format_currency(rr['AbsAmount'])}")
            lines.append("")

        # Summary table
        lines.append("-" * 80)
        lines.append("PART IX LINE 25 - TOTAL FUNCTIONAL EXPENSES")
        lines.append("-" * 80)
        lines.append(f"  (A) Total expenses:        {format_currency(total_expenses)}")
        lines.append(f"  (B) Program services:      {format_currency(prog_total)}")
        lines.append(f"  (C) Management & General:  {format_currency(mg_total)}")
        lines.append(f"  (D) Fundraising:           {format_currency(fund_total)}")
        lines.append("")

        # Missing tags warnings
        missing_exp = withdrawals[withdrawals["Form 990 Expense Line"].str.strip() == ""].copy()
        if not missing_exp.empty:
            lines.append("⚠️  ACTION REQUIRED: MISSING EXPENSE LINE TAGS")
            lines.append("-" * 50)
            lines.append(f"  Count: {len(missing_exp)} transactions")
            lines.append(f"  Total: {format_currency(missing_exp['AbsAmount'].sum())}")
            show = missing_exp.sort_values(["Month", "Date"]).head(25)
            for _, me in show.iterrows():
                lines.append(f"    {int(me['Month']):02d}/{me['Date']} | {format_currency(me['AbsAmount'])} | {me['Description'][:50]}")
            if len(missing_exp) > 25:
                lines.append(f"    ... and {len(missing_exp) - 25} more")
            lines.append("")

        missing_func = withdrawals[~withdrawals["__func_cat"].isin(VALID_FUNCTIONAL_CATEGORIES)].copy()
        if not missing_func.empty:
            lines.append("⚠️  ACTION REQUIRED: MISSING FUNCTIONAL CATEGORY")
            lines.append("-" * 50)
            lines.append(f"  Count: {len(missing_func)} transactions")
            lines.append(f"  Total: {format_currency(missing_func['AbsAmount'].sum())}")
            lines.append("  (Must assign Program, M&G, or Fundraising)")
            show = missing_func.sort_values(["Month", "Date"]).head(25)
            for _, mf in show.iterrows():
                lines.append(f"    {int(mf['Month']):02d}/{mf['Date']} | {format_currency(mf['AbsAmount'])} | {mf['Description'][:50]}")
            if len(missing_func) > 25:
                lines.append(f"    ... and {len(missing_func) - 25} more")
            lines.append("")

    # =========================================================================
    # DATA INTEGRITY CHECK
    # =========================================================================
    lines.append("=" * 80)
    lines.append("DATA INTEGRITY CHECK")
    lines.append("=" * 80)
    
    # Check that functional allocations sum correctly
    if not withdrawals.empty:
        func_sum = prog_total + mg_total + fund_total + unallocated
        if abs(func_sum - total_expenses) < 0.01:
            lines.append("✅ Functional allocations balance correctly")
        else:
            lines.append(f"⚠️  Functional allocations don't balance: {format_currency(func_sum)} vs {format_currency(total_expenses)}")
    
    # Check for missing tags
    missing_count = 0
    if not deposits.empty:
        missing_count += len(deposits[deposits["Form 990 Revenue Line"].str.strip() == ""])
    if not withdrawals.empty:
        missing_count += len(withdrawals[withdrawals["Form 990 Expense Line"].str.strip() == ""])
        missing_count += len(withdrawals[~withdrawals["__func_cat"].isin(VALID_FUNCTIONAL_CATEGORIES)])
    
    if missing_count == 0:
        lines.append("✅ All transactions have required Form 990 tags")
    else:
        lines.append(f"⚠️  {missing_count} transactions need tag corrections (see above)")

    lines.append("")
    lines.append("=" * 80)
    lines.append("End of Form 990 Worksheet Report")
    lines.append("=" * 80)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UI: Upload files
# ---------------------------------------------------------------------------

st.header("Step 1 — Upload Monthly Files")

uploaded_files = st.file_uploader(
    "Upload 1–12 monthly exports (CSV or Excel). You may upload in any order.",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("Upload at least one file to continue.")
    st.stop()

if len(uploaded_files) > 12:
    st.error("You may upload at most 12 monthly files.")
    st.stop()

# Read + validate each file
dfs: List[pd.DataFrame] = []
file_months: Dict[str, Tuple[int, int]] = {}  # filename -> (year, month) best-effort

for f in uploaded_files:
    df = read_upload(f)
    validate_schema(df, f.name)
    df = normalize_types(df)
    dfs.append(df)

    # best-effort: detect the file's month/year
    try:
        yy = int(df["Year"].dropna().unique()[0])
        mm = int(df["Month"].dropna().unique()[0])
        file_months[f.name] = (yy, mm)
    except Exception:
        pass

merged = pd.concat(dfs, ignore_index=True)

year = validate_single_year(merged)

# Deduplicate transactions
merged, removed = dedupe_transactions(merged)

months_present, months_missing = month_coverage(merged)

st.success(
    f"Loaded {len(uploaded_files)} file(s) for year {year}. "
    f"De-dup removed {removed} duplicate row(s)."
)
st.caption(
    f"Months present: {', '.join(map(str, months_present))}"
    + (f" | Missing: {', '.join(map(str, months_missing))}" if months_missing else " | All 12 months present")
)

# ---------------------------------------------------------------------------
# Data quality summary
# ---------------------------------------------------------------------------

with st.expander("Data Quality Summary"):
    st.subheader("Transaction Statistics")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Transactions", len(merged))
    with col2:
        st.metric("Total Revenue", format_currency(merged[merged["Amount"] > 0]["Amount"].sum()))
    with col3:
        st.metric("Total Expenses", format_currency(merged[merged["Amount"] < 0]["Amount"].abs().sum()))

    st.subheader("Tagging Completeness")

    # Check for missing tags
    missing_1023 = merged[merged["IRS Category Code (1023)"].str.strip() == ""]
    missing_990_rev = merged[(merged["Amount"] > 0) & (merged["Form 990 Revenue Line"].str.strip() == "")]
    missing_990_exp = merged[(merged["Amount"] < 0) & (merged["Form 990 Expense Line"].str.strip() == "")]
    missing_990_func = merged[(merged["Amount"] < 0) & (~merged["Form 990 Functional Category"].str.strip().isin(VALID_FUNCTIONAL_CATEGORIES))]
    flagged = merged[merged["Needs Further Investigation"] == True]

    col1, col2, col3 = st.columns(3)
    with col1:
        if len(missing_1023) > 0:
            st.warning(f"⚠️ {len(missing_1023)} transactions missing 1023 category")
        else:
            st.success("✅ All transactions have 1023 category")

    with col2:
        missing_990_count = len(missing_990_rev) + len(missing_990_exp)
        if missing_990_count > 0:
            st.warning(f"⚠️ {missing_990_count} transactions missing 990 line tags")
        else:
            st.success("✅ All transactions have 990 line tags")

    with col3:
        if len(missing_990_func) > 0:
            st.warning(f"⚠️ {len(missing_990_func)} expenses missing functional category")
        else:
            st.success("✅ All expenses have functional category")

    if len(flagged) > 0:
        st.info(f"ℹ️ {len(flagged)} transactions flagged for further investigation")

# ---------------------------------------------------------------------------
# Gala Revenue Adjustment (Stripe commingling fix)
# ---------------------------------------------------------------------------

st.header("Step 1b — Gala Revenue Adjustment")
st.write(
    "Stripe deposits often combine membership dues and Gala ticket sales into single transactions. "
    "Enter the **total Gala ticket revenue received via Stripe** for the year to properly allocate it."
)

if "gala_adjustment" not in st.session_state:
    st.session_state["gala_adjustment"] = 0.0

gala_input = st.number_input(
    "Total Gala ticket revenue via Stripe ($)",
    min_value=0.0,
    max_value=1000000.0,
    value=st.session_state["gala_adjustment"],
    step=100.0,
    help="This amount will be moved FROM membership dues TO fundraising event revenue in both reports."
)
st.session_state["gala_adjustment"] = gala_input

if gala_input > 0:
    st.info(
        f"📊 **Adjustment Preview:** ${gala_input:,.2f} will be reallocated:\n\n"
        f"- **Form 1023:** Category 2 (Membership fees) → Category 6 (Fundraising events)\n"
        f"- **Form 990:** Line 1b (Membership dues) → Line 8a (Fundraising event revenue)"
    )

# ---------------------------------------------------------------------------
# Optional: Preview merged data
# ---------------------------------------------------------------------------

with st.expander("Preview merged annual data (first 200 rows)"):
    st.dataframe(merged.head(200), use_container_width=True)

# ---------------------------------------------------------------------------
# Generate reports
# ---------------------------------------------------------------------------

st.header("Step 2 — Generate Annual Text Reports")

if "report_1023" not in st.session_state:
    st.session_state["report_1023"] = ""
if "report_990" not in st.session_state:
    st.session_state["report_990"] = ""

colA, colB = st.columns(2)

with colA:
    if st.button("Generate 1023 / 990-EZ Annual Text Report"):
        st.session_state["report_1023"] = build_report_1023(year, merged, st.session_state["gala_adjustment"])

with colB:
    if st.button("Generate Form 990 Smart Annual Text Report"):
        st.session_state["report_990"] = build_report_990(year, merged, st.session_state["gala_adjustment"])

# Convenience button
if st.button("Generate BOTH Reports"):
    st.session_state["report_1023"] = build_report_1023(year, merged, st.session_state["gala_adjustment"])
    st.session_state["report_990"] = build_report_990(year, merged, st.session_state["gala_adjustment"])

# ---------------------------------------------------------------------------
# Preview + downloads
# ---------------------------------------------------------------------------

st.header("Step 3 — Preview + Download Outputs")

if st.session_state["report_1023"]:
    st.subheader("Preview: Output 1 — 1023 / 990-EZ Style Annual Report")
    st.text_area("Output 1 Preview", st.session_state["report_1023"], height=450)

    st.download_button(
        "Download Output 1 (.txt)",
        data=st.session_state["report_1023"],
        file_name=f"FAOA_Annual_Report_{year}_1023_990EZ.txt",
        mime="text/plain",
    )

if st.session_state["report_990"]:
    st.subheader("Preview: Output 2 — Form 990 Smart Annual Report")
    st.text_area("Output 2 Preview", st.session_state["report_990"], height=450)

    st.download_button(
        "Download Output 2 (.txt)",
        data=st.session_state["report_990"],
        file_name=f"FAOA_Annual_Report_{year}_Form990_Smart.txt",
        mime="text/plain",
    )

# Optional merged CSV download
merged_csv = merged.to_csv(index=False)
st.download_button(
    "Download Merged Annual CSV (optional)",
    data=merged_csv,
    file_name=f"FAOA_Merged_Annual_{year}.csv",
    mime="text/csv",
)
