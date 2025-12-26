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
    "Upload 1–12 monthly CSVs from the FAOA Monthly Treasurer Tool to generate "
    "an annual roll-up, adjust IRS category totals (including Gala ticket reclass), "
    "and produce a formatted annual text report."
)

# ---------------------------------------------------------------------------
# Constants / Canonical Labels
# ---------------------------------------------------------------------------

REVENUE_CODES = {"1", "2", "3", "4", "6", "7", "9"}
EXPENSE_CODES = {"14", "15", "16", "18", "19", "22", "23"}
ALL_CODES = REVENUE_CODES | EXPENSE_CODES

# Canonical labels (used only if a code is missing from the uploaded data but we need to show it)
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

HARD_REQUIRED_COLUMNS = {
    "Year",
    "Month",
    "Amount",
    "IRS Category Code",
    "IRS Category Label",
}

OPTIONAL_COLUMNS_WITH_DEFAULTS = {
    "Date": "",
    "Description": "",
    "Itemization Label": "",
    "Member/Event Label": "",
    "Event Location": "",
    "Event Purpose": "",
    "Sponsor Name": "",
    "Potential Sponsorship": False,
    "Needs Further Investigation": False,
}

PROFESSIONAL_FEES_EXPLANATION = (
    "Professional fees include external professional services and recurring software platforms "
    "necessary for FAOA operations, including legal and accounting services; consulting support; "
    "and SaaS tools for website hosting, membership management, FAO Connect, communications, "
    "email services, and payment processing."
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def format_currency(value: float) -> str:
    if pd.isna(value):
        return "$0.00"
    return f"${value:,.2f}"


def coerce_bool_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin(["true", "1", "yes", "y"])


def clean_str_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    missing_hard = HARD_REQUIRED_COLUMNS - set(df.columns)
    if missing_hard:
        st.error(
            f"Missing required columns: {', '.join(sorted(missing_hard))}. "
            "Please use exports from the monthly FAOA tool."
        )
        st.stop()

    for col, default in OPTIONAL_COLUMNS_WITH_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    for col in ["Year", "Month", "Amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df[["Year", "Month", "Amount"]].isna().any().any():
        st.error("Invalid numeric values detected in Year, Month, or Amount.")
        st.stop()

    for col in [
        "Date",
        "Description",
        "Itemization Label",
        "Member/Event Label",
        "Event Location",
        "Event Purpose",
        "Sponsor Name",
        "IRS Category Code",
        "IRS Category Label",
    ]:
        if col in df.columns:
            df[col] = clean_str_series(df[col])

    for col in ["Potential Sponsorship", "Needs Further Investigation"]:
        if col in df.columns:
            df[col] = coerce_bool_series(df[col])
        else:
            df[col] = False

    return df


def validate_year(df: pd.DataFrame) -> int:
    years = df["Year"].dropna().unique()
    if len(years) != 1:
        st.error(f"All files must be from one year. Found: {sorted(years)}")
        st.stop()
    return int(years[0])


def validate_categories(df: pd.DataFrame):
    codes = set(df["IRS Category Code"].astype(str).unique())
    unknown = codes - ALL_CODES
    if unknown:
        st.error("Unexpected IRS Category Codes: " + ", ".join(sorted(unknown)))
        st.stop()


def build_summary_table(df: pd.DataFrame) -> pd.DataFrame:
    summary = (
        df.groupby(["IRS Category Code", "IRS Category Label"])["Amount"]
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


def build_annual_report(year: int, summary_df: pd.DataFrame, full_df: pd.DataFrame, gala_ticket_amount: float) -> str:
    lines = []

    lines.append(f"{year} Foreign Area Officer Association Annual Financial Report")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("------------------------------------------------------------")
    lines.append("")

    # Revenue Categories (Adjusted)
    lines.append("REVENUE CATEGORIES")
    rev_summary = summary_df[summary_df["IRS Category Code"].isin(REVENUE_CODES)].copy()
    rev_summary["__sort"] = pd.to_numeric(rev_summary["IRS Category Code"], errors="coerce")
    rev_summary = rev_summary.sort_values("__sort").drop(columns="__sort")
    for _, r in rev_summary.iterrows():
        lines.append(
            f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
            f"{format_currency(r['Adjusted Total Amount'])}"
        )

    lines.append("")

    # Expense Categories (Adjusted)
    lines.append("EXPENSE CATEGORIES")
    exp_summary = summary_df[summary_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()
    exp_summary["__sort"] = pd.to_numeric(exp_summary["IRS Category Code"], errors="coerce")
    exp_summary = exp_summary.sort_values("__sort").drop(columns="__sort")
    for _, r in exp_summary.iterrows():
        lines.append(
            f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
            f"{format_currency(r['Adjusted Total Amount'])}"
        )

    # Itemized Revenue (include Gala Tickets line under Category 9)
    lines.append("")
    lines.append("ITEMIZED REVENUE")
    lines.append("")

    gala_ticket_amount = float(gala_ticket_amount or 0.0)

    rev_df = full_df[full_df["IRS Category Code"].isin(REVENUE_CODES)].copy()

    # Category 1 special: Sponsor Name grouping (if present)
    cat1 = rev_df[rev_df["IRS Category Code"] == "1"].copy()
    if not cat1.empty:
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
            # Fall back to Itemization Label grouping
            cat1["Itemization Label"] = clean_str_series(cat1["Itemization Label"]).replace("", "UNLABELED")
            group = (
                cat1.groupby("Itemization Label")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization Label")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # Other revenue categories (force Category 9 section if Gala Ticket amount provided)
    for code in sorted(REVENUE_CODES - {"1"}, key=int):
        cat_df = rev_df[rev_df["IRS Category Code"] == code].copy()

        if cat_df.empty and not (code == "9" and gala_ticket_amount > 0.0):
            continue

        label = cat_df["IRS Category Label"].iloc[0] if not cat_df.empty else CATEGORY_LABELS.get(code, "")
        lines.append(f"  Category {code} – {label}:")

        if code == "9" and gala_ticket_amount > 0.0:
            lines.append(f"    Gala Tickets: {format_currency(gala_ticket_amount)}")

        if not cat_df.empty:
            cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"]).replace("", "UNLABELED")
            group = (
                cat_df.groupby("Itemization Label")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization Label")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # Itemized Expenses (put the Professional Fees explanation inside Category 22; no UNLABELED there)
    lines.append("")
    lines.append("ITEMIZED EXPENSES")
    lines.append("")

    exp_df = full_df[full_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()

    for code in sorted(EXPENSE_CODES, key=int):
        cat_df = exp_df[exp_df["IRS Category Code"] == code].copy()
        if cat_df.empty:
            continue

        label = cat_df["IRS Category Label"].iloc[0]
        lines.append(f"  Category {code} – {label}:")

        # Category 22: include explanation here; suppress "UNLABELED"
        if code == "22":
            lines.append(f"    {PROFESSIONAL_FEES_EXPLANATION}")
            lines.append("")  # breathing room

            # If there are usable Itemization Labels, show them; otherwise show a clean total line.
            cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"])
            has_any_labels = cat_df["Itemization Label"].str.strip().ne("").any()

            if has_any_labels:
                group = (
                    cat_df[cat_df["Itemization Label"].str.strip().ne("")]
                    .groupby("Itemization Label")["Amount"]
                    .sum()
                    .reset_index()
                    .sort_values("Itemization Label")
                )
                for _, r in group.iterrows():
                    lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")
            else:
                total = float(cat_df["Amount"].sum())
                lines.append(f"    Total: {format_currency(total)}")

            continue

        # All other expense categories: keep UNLABELED behavior
        cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"]).replace("", "UNLABELED")
        grouped = (
            cat_df.groupby("Itemization Label")["Amount"]
            .sum()
            .reset_index()
            .sort_values("Itemization Label")
        )

        for _, r in grouped.iterrows():
            lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    lines.append("")
    lines.append("End of report.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Upload + Gala reclass + Summary editor + Generate
# ---------------------------------------------------------------------------

st.header("Step 1 – Upload Monthly CSVs")

uploaded_files = st.file_uploader(
    "Upload 1–12 monthly CSVs",
    type=["csv"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("Please upload at least one CSV.")
    st.stop()

if len(uploaded_files) > 12:
    st.error("You may upload at most 12 monthly CSVs.")
    st.stop()

dfs = []
for f in uploaded_files:
    try:
        df = pd.read_csv(f)
    except Exception as e:
        st.error(f"Error reading file '{f.name}': {e}")
        st.stop()
    df = ensure_columns(df)
    dfs.append(df)

full_df = pd.concat(dfs, ignore_index=True)

year = validate_year(full_df)
validate_categories(full_df)

st.success(f"Loaded {len(uploaded_files)} file(s) for year {year}.")
st.subheader(f"Annual Report – {year}")

# Step 1B – Gala Ticket Reclassification (2 -> 9)
st.header("Step 1B – Gala Ticket Reclassification (Category 2 → Category 9)")

cat2_raw_total = float(full_df.loc[full_df["IRS Category Code"] == "2", "Amount"].sum())

st.write(
    'Is any **Gala Ticket revenue** currently embedded within **Category 2 - Membership fees received** '
    '(because it all comes in through Stripe)? If yes, enter the Gala Ticket amount below (may be zero). '
    'This will **subtract from Category 2** and **add to Category 9** (Adjusted totals only).'
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

# Step 2 – Annual Summary
st.header("Step 2 – Annual Summary by IRS Category")

summary_df = build_summary_table(full_df)
summary_df = apply_gala_ticket_reclass(summary_df, float(st.session_state.get("gala_ticket_amount", 0.0)))

st.write(
    "Review the annual totals below. You may edit the **Adjusted Total Amount** column to apply year-end corrections. "
    "Raw totals come directly from uploaded data.\n\n"
    "Note: The Gala Ticket reclassification above has already been applied to the **Adjusted** totals for "
    "Category 2 and Category 9."
)

edited_summary_df = st.data_editor(
    summary_df,
    num_rows="fixed",
    disabled=["IRS Category Code", "IRS Category Label", "Raw Total Amount"],
    key="annual_summary_editor",
)

# Step 3 – Generate
st.header("Step 3 – Generate Annual Text Report")

if "annual_report_text" not in st.session_state:
    st.session_state["annual_report_text"] = ""

if st.button("Generate Annual Report"):
    st.session_state["annual_report_text"] = build_annual_report(
        year=year,
        summary_df=edited_summary_df,
        full_df=full_df,
        gala_ticket_amount=float(st.session_state.get("gala_ticket_amount", 0.0)),
    )

if st.session_state["annual_report_text"]:
    st.subheader("Preview – Annual Text Report")
    st.text_area(
        "Annual Report (read-only preview)",
        st.session_state["annual_report_text"],
        height=500,
    )

    st.header("Step 4 – Download Outputs")

    st.download_button(
        "Download Report (.txt)",
        st.session_state["annual_report_text"],
        f"FAOA_Annual_Financial_Report_{year}.txt",
        "text/plain",
    )

    summary_csv = edited_summary_df.to_csv(index=False)
    st.download_button(
        "Download Adjusted Annual Summary (.csv)",
        summary_csv,
        f"FAOA_Annual_Summary_{year}.csv",
        "text/csv",
    )
