# app.py

import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# Basic page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="FAOA Annual Report Generator",
    layout="wide",
)

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
        st.title("FAOA Annual Report Generator")
        st.write("This tool is password protected.")
        password = st.text_input("Enter password", type="password")

        if password == "":
            st.stop()

        if password == st.secrets[secret_key]:
            st.session_state["password_correct"] = True
        else:
            st.error("Incorrect password.")
            st.stop()


check_password()  # block everything below until password is correct

# ---------------------------------------------------------------------------
# Main title
# ---------------------------------------------------------------------------

st.title("FAOA Annual Report Generator")
st.write(
    "Upload 1–12 monthly exports to generate two annual text reports:\n"
    "1) An IRS/1023-style annual report (by IRS Category Codes + itemization), and\n"
    "2) A Form 990 expense rollup report (by Form 990 Expense Line)."
)

# ---------------------------------------------------------------------------
# Constants / Labels
# ---------------------------------------------------------------------------

REVENUE_CODES = {"1", "2", "3", "4", "6", "7", "9"}
EXPENSE_CODES = {"14", "15", "16", "18", "19", "22", "23"}
ALL_CODES = REVENUE_CODES | EXPENSE_CODES

# Canonical IRS labels (used if a code is missing)
CATEGORY_LABELS = {
    "1": "Gifts, grants, contributions received",
    "2": "Membership fees received",
    "3": "Gross sales of inventory",
    "4": "Other revenue",
    "6": "Investment income",
    "7": "Other revenue",
    "9": "Gross receipts from activities related to exempt purpose",
    "14": "Fundraising expenses",
    "15": "Contributions, gifts, grants paid out",
    "16": "Disbursements to/for members",
    "18": "Office expenses",
    "19": "Travel",
    "22": "Professional fees",
    "23": "Other expenses not classified above",
}

# Strict professional-fees itemization labels allowed
ALLOWED_PROFESSIONAL_FEE_LABELS = {
    "Professional Fees (IT Services)",
    "Professional Fees (Accounting Fees)",
    "Professional Fees (Legal Fees)",
    "Professional Fees (Marketing/Advertising)",
    "Professional Fees (Services)",
}

PROFESSIONAL_FEES_EXPLANATION = (
    "Professional fees include external professional services and recurring software platforms "
    "necessary for FAOA operations, including legal and accounting services; consulting support; "
    "and SaaS tools for website hosting, membership management, FAO Connect, communications, "
    "email services, and payment processing."
)

# Required columns expected in the NEW monthly export format
HARD_REQUIRED_COLUMNS = {
    "Year",
    "Month",
    "Amount",
    "IRS Category Code",
    "IRS Category Label",
}

# Columns that often exist in the new export; we’ll create defaults if missing
OPTIONAL_COLUMNS_WITH_DEFAULTS = {
    "Date": "",
    "Description": "",
    "Form 990 Expense Line": "",
    "Itemization Preset": "",
    "Itemization Label": "",
    "Member/Event Label": "",
    "Event Location": "",
    "Event Purpose": "",
    "Sponsor Name": "",
    "Potential Sponsorship": False,
    "Needs Further Investigation": False,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_currency(value: float) -> str:
    if pd.isna(value):
        return "$0.00"
    return f"${value:,.2f}"


def clean_str_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def coerce_bool_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def read_uploaded_file(f) -> pd.DataFrame:
    name = (f.name or "").lower()
    try:
        if name.endswith(".xlsx") or name.endswith(".xls"):
            return pd.read_excel(f)
        return pd.read_csv(f)
    except Exception as e:
        st.error(f"Error reading file '{f.name}': {e}")
        st.stop()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    missing = HARD_REQUIRED_COLUMNS - set(df.columns)
    if missing:
        st.error(
            f"Missing required columns: {', '.join(sorted(missing))}. "
            "Please upload exports from the FAOA Monthly Treasurer Tool (new format)."
        )
        st.stop()

    # Add optional columns if missing
    for col, default in OPTIONAL_COLUMNS_WITH_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    # Numeric coercion
    for col in ["Year", "Month", "Amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df[["Year", "Month", "Amount"]].isna().any().any():
        st.error("Invalid numeric values detected in Year, Month, or Amount.")
        st.stop()

    # Clean strings
    str_cols = [
        "Date",
        "Description",
        "IRS Category Code",
        "IRS Category Label",
        "Form 990 Expense Line",
        "Itemization Preset",
        "Itemization Label",
        "Member/Event Label",
        "Event Location",
        "Event Purpose",
        "Sponsor Name",
    ]
    for c in str_cols:
        if c in df.columns:
            df[c] = clean_str_series(df[c])

    # Booleans
    for c in ["Potential Sponsorship", "Needs Further Investigation"]:
        if c in df.columns:
            df[c] = coerce_bool_series(df[c])
        else:
            df[c] = False

    # Force codes to string for grouping consistency
    df["IRS Category Code"] = df["IRS Category Code"].astype(str).str.strip()

    return df


def validate_year(df: pd.DataFrame) -> int:
    years = sorted(df["Year"].dropna().unique())
    if len(years) != 1:
        st.error(f"All uploaded files must be from one year. Found years: {years}")
        st.stop()
    return int(years[0])


def validate_categories(df: pd.DataFrame):
    codes = set(df["IRS Category Code"].astype(str).unique())
    unknown = codes - ALL_CODES
    if unknown:
        st.error("Unexpected IRS Category Codes: " + ", ".join(sorted(unknown)))
        st.stop()


def normalize_itemization_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    - Replace blank Itemization Label with 'Not itemized' (for reporting only).
    """
    df = df.copy()
    df["Itemization Label"] = clean_str_series(df["Itemization Label"])
    df.loc[df["Itemization Label"] == "", "Itemization Label"] = "Not itemized"
    return df


def enforce_professional_fees_rules(df: pd.DataFrame) -> pd.DataFrame:
    """
    If a transaction is a professional fee (based on the Itemization Label prefix),
    then force IRS Category Code to 22 and enforce allowed labels.
    Invalid professional-fee labels are flagged for investigation.
    """
    df = df.copy()

    # Identify professional fees by label pattern
    label = clean_str_series(df["Itemization Label"])
    is_prof_fee = label.str.startswith("Professional Fees (", na=False)

    # Force recode to Category 22
    df.loc[is_prof_fee, "IRS Category Code"] = "22"
    df.loc[is_prof_fee, "IRS Category Label"] = "Professional fees"

    # Validate allowed professional-fee labels
    invalid_prof_fee = is_prof_fee & (~label.isin(ALLOWED_PROFESSIONAL_FEE_LABELS))
    if invalid_prof_fee.any():
        df.loc[invalid_prof_fee, "Needs Further Investigation"] = True

    return df


def build_summary_table(df: pd.DataFrame) -> pd.DataFrame:
    summary = (
        df.groupby(["IRS Category Code", "IRS Category Label"], dropna=False)["Amount"]
        .sum()
        .reset_index(name="Raw Total Amount")
    )
    summary["Adjusted Total Amount"] = summary["Raw Total Amount"]
    summary["__sort"] = pd.to_numeric(summary["IRS Category Code"], errors="coerce")
    summary = summary.sort_values("__sort").drop(columns="__sort").reset_index(drop=True)
    return summary


def ensure_category_rows_exist(summary_df: pd.DataFrame, codes_needed: set) -> pd.DataFrame:
    existing = set(summary_df["IRS Category Code"].astype(str).unique())
    missing = {c for c in codes_needed if c not in existing}
    if not missing:
        return summary_df

    new_rows = []
    for code in sorted(missing, key=lambda x: int(x)):
        new_rows.append({
            "IRS Category Code": code,
            "IRS Category Label": CATEGORY_LABELS.get(code, ""),
            "Raw Total Amount": 0.0,
            "Adjusted Total Amount": 0.0,
        })

    combined = pd.concat([summary_df, pd.DataFrame(new_rows)], ignore_index=True)
    combined["__sort"] = pd.to_numeric(combined["IRS Category Code"], errors="coerce")
    combined = combined.sort_values("__sort").drop(columns="__sort").reset_index(drop=True)
    return combined


def apply_gala_ticket_reclass(summary_df: pd.DataFrame, gala_amount: float) -> pd.DataFrame:
    """
    Subtract gala_amount from Adjusted Total for category 2,
    add gala_amount to Adjusted Total for category 9.
    Raw totals remain unchanged.
    """
    summary_df = ensure_category_rows_exist(summary_df, {"2", "9"})

    gala_amount = float(gala_amount or 0.0)
    if gala_amount < 0:
        st.error("Gala ticket amount cannot be negative.")
        st.stop()

    idx2 = summary_df.index[summary_df["IRS Category Code"] == "2"].tolist()
    idx9 = summary_df.index[summary_df["IRS Category Code"] == "9"].tolist()

    raw2 = float(summary_df.loc[idx2[0], "Raw Total Amount"]) if idx2 else 0.0

    if gala_amount > raw2 + 1e-9:
        st.error(
            f"Gala ticket amount ({format_currency(gala_amount)}) cannot exceed the raw total for "
            f'Category 2 ({format_currency(raw2)}).'
        )
        st.stop()

    summary_df.loc[idx2[0], "Adjusted Total Amount"] = float(summary_df.loc[idx2[0], "Adjusted Total Amount"]) - gala_amount
    summary_df.loc[idx9[0], "Adjusted Total Amount"] = float(summary_df.loc[idx9[0], "Adjusted Total Amount"]) + gala_amount

    return summary_df


def group_amounts_by_label(cat_df: pd.DataFrame) -> pd.DataFrame:
    """
    Group by Itemization Label and return sorted totals.
    Assumes Itemization Label already normalized (no blanks).
    """
    g = (
        cat_df.groupby("Itemization Label")["Amount"]
        .sum()
        .reset_index()
        .sort_values("Itemization Label")
    )
    return g


# ---------------------------------------------------------------------------
# REPORT A: IRS/1023-style Annual Report (IRS Category rollup + itemization)
# ---------------------------------------------------------------------------

def build_report_a_annual_irs(year: int, summary_df: pd.DataFrame, full_df: pd.DataFrame, gala_ticket_amount: float) -> str:
    lines = []

    lines.append(f"{year} Foreign Area Officer Association Annual Financial Report")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("------------------------------------------------------------")
    lines.append("")

    # Revenue summary (Adjusted)
    lines.append("REVENUE CATEGORIES")
    rev_summary = summary_df[summary_df["IRS Category Code"].isin(REVENUE_CODES)].copy()
    rev_summary["__sort"] = pd.to_numeric(rev_summary["IRS Category Code"], errors="coerce")
    rev_summary = rev_summary.sort_values("__sort").drop(columns="__sort")
    if rev_summary.empty:
        lines.append("  (No revenue recorded for this period.)")
    else:
        for _, r in rev_summary.iterrows():
            lines.append(
                f"  {r['IRS Category Code']} - {r['IRS Category Label']}: {format_currency(r['Adjusted Total Amount'])}"
            )

    lines.append("")

    # Expense summary (Adjusted)
    lines.append("EXPENSE CATEGORIES")
    exp_summary = summary_df[summary_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()
    exp_summary["__sort"] = pd.to_numeric(exp_summary["IRS Category Code"], errors="coerce")
    exp_summary = exp_summary.sort_values("__sort").drop(columns="__sort")
    if exp_summary.empty:
        lines.append("  (No expenses recorded for this period.)")
    else:
        for _, r in exp_summary.iterrows():
            lines.append(
                f"  {r['IRS Category Code']} - {r['IRS Category Label']}: {format_currency(r['Adjusted Total Amount'])}"
            )

    # Itemized revenue
    lines.append("")
    lines.append("ITEMIZED REVENUE")
    lines.append("")

    gala_ticket_amount = float(gala_ticket_amount or 0.0)

    rev_df = full_df[full_df["IRS Category Code"].isin(REVENUE_CODES)].copy()
    rev_df = normalize_itemization_labels(rev_df)

    any_rev = False

    # Sponsors (Cat 1): prefer Sponsor Name grouping
    cat1 = rev_df[rev_df["IRS Category Code"] == "1"].copy()
    if not cat1.empty:
        any_rev = True
        label = cat1["IRS Category Label"].iloc[0]
        lines.append(f"  Category 1 – {label}:")
        if cat1["Sponsor Name"].str.strip().ne("").any():
            sponsor_group = (
                cat1[cat1["Sponsor Name"].str.strip() != ""]
                .groupby("Sponsor Name")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Sponsor Name")
            )
            for _, r in sponsor_group.iterrows():
                lines.append(f"    {r['Sponsor Name']}: {format_currency(r['Amount'])}")
        else:
            grouped = group_amounts_by_label(cat1)
            for _, r in grouped.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # Other revenue codes (force Cat 9 if gala amount > 0)
    for code in sorted(REVENUE_CODES - {"1"}, key=int):
        cat_df = rev_df[rev_df["IRS Category Code"] == code].copy()

        if cat_df.empty and not (code == "9" and gala_ticket_amount > 0.0):
            continue

        any_rev = True
        label = cat_df["IRS Category Label"].iloc[0] if not cat_df.empty else CATEGORY_LABELS.get(code, "")
        lines.append(f"  Category {code} – {label}:")

        if code == "9" and gala_ticket_amount > 0.0:
            lines.append(f"    Gala Tickets: {format_currency(gala_ticket_amount)}")

        if not cat_df.empty:
            grouped = group_amounts_by_label(cat_df)
            for _, r in grouped.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    if not any_rev:
        lines.append("  (No itemized revenue entries.)")

    # Itemized expenses
    lines.append("")
    lines.append("ITEMIZED EXPENSES")
    lines.append("")

    exp_df = full_df[full_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()
    exp_df = normalize_itemization_labels(exp_df)

    if exp_df.empty:
        lines.append("  (No itemized expense entries.)")
    else:
        for code in sorted(EXPENSE_CODES, key=int):
            cat_df = exp_df[exp_df["IRS Category Code"] == code].copy()
            if cat_df.empty:
                continue

            label = cat_df["IRS Category Label"].iloc[0]
            lines.append(f"  Category {code} – {label}:")

            # Put the professional-fees explanation INSIDE category 22
            if code == "22":
                lines.append(f"    {PROFESSIONAL_FEES_EXPLANATION}")

                # Ensure professional-fee labels are present and valid where applicable
                # (If blank, it will show as "Not itemized" and should be fixed upstream)
                lines.append("")

            grouped = group_amounts_by_label(cat_df)
            for _, r in grouped.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # Needs Further Investigation
    lines.append("")
    lines.append("NEEDS FURTHER INVESTIGATION (Treasurer Flagged / Validation Flags)")
    flagged = full_df[full_df["Needs Further Investigation"] == True].copy()
    if flagged.empty:
        lines.append("  (None flagged this period.)")
    else:
        lines.append(f"  Count of flagged transactions: {len(flagged)}")
        lines.append(f"  Net total of flagged amounts: {format_currency(flagged['Amount'].sum())}")
        # Light detail (no sensitive dump)
        flagged = normalize_itemization_labels(flagged)
        sample = flagged[["Date", "Description", "Amount", "IRS Category Code", "Itemization Label"]].head(20)
        lines.append("  Sample (first 20):")
        for _, r in sample.iterrows():
            lines.append(
                f"    {r['Date']} | {r['IRS Category Code']} | {r['Itemization Label']} | {format_currency(r['Amount'])} | {r['Description']}"
            )

    lines.append("")
    lines.append("End of report.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# REPORT B: Form 990 Expense Rollup Report (by Form 990 Expense Line)
# ---------------------------------------------------------------------------

def build_report_b_form_990(year: int, full_df: pd.DataFrame) -> str:
    lines = []

    lines.append(f"{year} FAOA Form 990 Expense Rollup Report")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("------------------------------------------------------------")
    lines.append("")
    lines.append("This report consolidates EXPENSES by the 'Form 990 Expense Line' field from the monthly exports.")
    lines.append("")

    exp_df = full_df[full_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()
    if exp_df.empty:
        lines.append("(No expenses recorded for this period.)")
        lines.append("")
        lines.append("End of report.")
        return "\n".join(lines)

    exp_df = normalize_itemization_labels(exp_df)
    exp_df["Form 990 Expense Line"] = clean_str_series(exp_df["Form 990 Expense Line"])
    exp_df.loc[exp_df["Form 990 Expense Line"] == "", "Form 990 Expense Line"] = "UNASSIGNED (Needs review)"

    # Rollup totals by 990 line
    rollup = (
        exp_df.groupby("Form 990 Expense Line")["Amount"]
        .sum()
        .reset_index()
        .sort_values("Form 990 Expense Line")
    )

    for _, row in rollup.iterrows():
        line_name = row["Form 990 Expense Line"]
        total = row["Amount"]
        lines.append(f"{line_name}: {format_currency(total)}")

        line_df = exp_df[exp_df["Form 990 Expense Line"] == line_name].copy()

        # Itemize within each 990 line by Itemization Label
        grouped = group_amounts_by_label(line_df)
        for _, r in grouped.iterrows():
            lines.append(f"  {r['Itemization Label']}: {format_currency(r['Amount'])}")

        lines.append("")

    lines.append("End of report.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UI: Upload
# ---------------------------------------------------------------------------

st.header("Step 1 – Upload Monthly Files (CSV or Excel)")

uploaded_files = st.file_uploader(
    "Upload 1–12 monthly exports from the FAOA Monthly Treasurer Tool:",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("Please upload at least one file.")
    st.stop()

if len(uploaded_files) > 12:
    st.error("You may upload at most 12 monthly files.")
    st.stop()

dfs = []
for f in uploaded_files:
    df = read_uploaded_file(f)
    df = ensure_columns(df)
    dfs.append(df)

full_df = pd.concat(dfs, ignore_index=True)

year = validate_year(full_df)
validate_categories(full_df)

# Enforce / normalize professional fees rules BEFORE summaries and reports
full_df = enforce_professional_fees_rules(full_df)

st.success(f"Loaded {len(uploaded_files)} file(s) for year {year}.")

# ---------------------------------------------------------------------------
# UI: Gala Ticket Reclassification (2 -> 9)
# ---------------------------------------------------------------------------

st.header("Step 2 – Gala Ticket Reclassification (Category 2 → Category 9)")

cat2_raw_total = float(full_df.loc[full_df["IRS Category Code"] == "2", "Amount"].sum())

st.write(
    "If Stripe combined **Gala Ticket revenue** into **Category 2 - Membership fees received**, enter the Gala Ticket amount below.\n"
    "This amount will be **subtracted from Category 2** and **added to Category 9** (Adjusted totals only), and will appear as an itemized line under Category 9."
)

gala_ticket_amount = st.number_input(
    "Gala ticket amount to reclassify (USD)",
    min_value=0.0,
    value=float(st.session_state.get("gala_ticket_amount", 0.0)),
    step=10.0,
    format="%.2f",
    help=f"Raw total currently in Category 2 is {format_currency(cat2_raw_total)}.",
)
st.session_state["gala_ticket_amount"] = float(gala_ticket_amount)

st.caption(
    f"Category 2 raw total: {format_currency(cat2_raw_total)} • "
    f"Reclass amount: {format_currency(gala_ticket_amount)} • "
    f"Net Category 2 after reclass (Adjusted only): {format_currency(cat2_raw_total - gala_ticket_amount)}"
)

# ---------------------------------------------------------------------------
# UI: Annual Summary (Adjusted totals editable)
# ---------------------------------------------------------------------------

st.header("Step 3 – Annual Summary by IRS Category (Editable Adjusted Totals)")

summary_df = build_summary_table(full_df)
summary_df = apply_gala_ticket_reclass(summary_df, float(st.session_state.get("gala_ticket_amount", 0.0)))

st.write(
    "Review the annual totals below. You may edit **Adjusted Total Amount** to apply year-end corrections.\n\n"
    "Note: Gala Ticket reclassification has already been applied to the **Adjusted** totals for Category 2 and Category 9."
)

edited_summary_df = st.data_editor(
    summary_df,
    num_rows="fixed",
    disabled=["IRS Category Code", "IRS Category Label", "Raw Total Amount"],
    key="annual_summary_editor",
)

# ---------------------------------------------------------------------------
# UI: Generate Reports
# ---------------------------------------------------------------------------

st.header("Step 4 – Generate Text Reports")

if "report_a_text" not in st.session_state:
    st.session_state["report_a_text"] = ""
if "report_b_text" not in st.session_state:
    st.session_state["report_b_text"] = ""

if st.button("Generate Both Reports"):
    st.session_state["report_a_text"] = build_report_a_annual_irs(
        year=year,
        summary_df=edited_summary_df,
        full_df=full_df,
        gala_ticket_amount=float(st.session_state.get("gala_ticket_amount", 0.0)),
    )
    st.session_state["report_b_text"] = build_report_b_form_990(
        year=year,
        full_df=full_df,
    )

# ---------------------------------------------------------------------------
# UI: Preview + Downloads
# ---------------------------------------------------------------------------

if st.session_state["report_a_text"] or st.session_state["report_b_text"]:
    st.subheader("Preview – Report A (IRS/1023-style Annual Report)")
    st.text_area(
        "Report A (preview)",
        value=st.session_state["report_a_text"],
        height=450,
    )

    st.subheader("Preview – Report B (Form 990 Expense Rollup)")
    st.text_area(
        "Report B (preview)",
        value=st.session_state["report_b_text"],
        height=450,
    )

    st.header("Step 5 – Download Outputs")

    st.download_button(
        "Download Report A (.txt)",
        data=st.session_state["report_a_text"],
        file_name=f"FAOA_Annual_Financial_Report_{year}_ReportA_IRS.txt",
        mime="text/plain",
    )

    st.download_button(
        "Download Report B (.txt)",
        data=st.session_state["report_b_text"],
        file_name=f"FAOA_Annual_Financial_Report_{year}_ReportB_Form990.txt",
        mime="text/plain",
    )

    adjusted_summary_csv = edited_summary_df.to_csv(index=False)
    st.download_button(
        "Download Adjusted Annual Summary (.csv)",
        data=adjusted_summary_csv,
        file_name=f"FAOA_Annual_Summary_{year}_Adjusted.csv",
        mime="text/csv",
    )
