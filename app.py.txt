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

    If you want a different key name, change "APP_PASSWORD" below.
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
    "an annual roll-up, adjust IRS category totals, and produce a formatted annual text report."
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVENUE_CODES = {"1", "2", "3", "4", "6", "7", "9"}
EXPENSE_CODES = {"14", "15", "16", "18", "19", "22", "23"}
ALL_CODES = REVENUE_CODES | EXPENSE_CODES

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

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def format_currency(value: float) -> str:
    """Format a float as currency with 2 decimals."""
    if pd.isna(value):
        return "$0.00"
    return f"${value:,.2f}"


def coerce_bool_series(series: pd.Series) -> pd.Series:
    """Coerce a series to boolean from typical CSV encodings."""
    return series.astype(str).str.strip().str.lower().isin(
        ["true", "1", "yes", "y"]
    )


def clean_str_series(series: pd.Series) -> pd.Series:
    """Strip whitespace and replace NaN with empty string."""
    return series.fillna("").astype(str).str.strip()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all required and optional columns exist; create defaults where needed."""
    missing_hard = HARD_REQUIRED_COLUMNS - set(df.columns)
    if missing_hard:
        st.error(
            f"Missing required columns in uploaded CSV(s): {', '.join(sorted(missing_hard))}. "
            "Please ensure you are using exports from the monthly FAOA tool."
        )
        st.stop()

    # Add optional columns with defaults if missing
    for col, default in OPTIONAL_COLUMNS_WITH_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    # Coerce numeric columns
    for col in ["Year", "Month", "Amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df["Year"].isna().any():
        st.error("Some rows have invalid Year values.")
        st.stop()

    if df["Month"].isna().any():
        st.error("Some rows have invalid Month values.")
        st.stop()

    if df["Amount"].isna().any():
        st.error("Some rows have invalid Amount values.")
        st.stop()

    # Clean string-like columns
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

    # Coerce booleans
    for col in ["Potential Sponsorship", "Needs Further Investigation"]:
        if col in df.columns:
            df[col] = coerce_bool_series(df[col])
        else:
            df[col] = False

    return df


def validate_year(df: pd.DataFrame) -> int:
    """Ensure all rows are for a single year and return that year."""
    years = df["Year"].dropna().unique()
    if len(years) != 1:
        st.error(f"Uploaded CSVs must all belong to the same year. Found years: {sorted(years)}.")
        st.stop()
    return int(years[0])


def validate_categories(df: pd.DataFrame):
    """Ensure IRS Category Code values are within the allowed set."""
    codes = set(df["IRS Category Code"].unique())
    unknown = codes - ALL_CODES
    if unknown:
        st.error(
            "Unexpected IRS Category Codes found: " + ", ".join(sorted(unknown))
        )
        st.stop()


def build_summary_table(df: pd.DataFrame) -> pd.DataFrame:
    """Build annual summary with raw totals and editable adjusted totals."""
    summary = (
        df.groupby(["IRS Category Code", "IRS Category Label"], dropna=False)["Amount"]
        .sum()
        .reset_index(name="Raw Total Amount")
    )
    summary["Adjusted Total Amount"] = summary["Raw Total Amount"]

    summary["__code_int"] = pd.to_numeric(summary["IRS Category Code"], errors="coerce")
    summary = summary.sort_values("__code_int").drop(columns="__code_int")
    return summary


def show_month_coverage(df: pd.DataFrame):
    """Display which months are present and which are missing in the uploaded data."""
    st.subheader("Month Coverage Check")

    months = df["Month"].astype(int)
    unique_months = sorted(set(m for m in months if 1 <= m <= 12))

    if not unique_months:
        st.warning("No valid months detected in the uploaded data.")
        return

    st.write(f"Months present: **{', '.join(map(str, unique_months))}**")

    missing = [m for m in range(1, 13) if m not in unique_months]
    if missing:
        st.warning(
            "Missing months: " + ", ".join(map(str, missing)) +
            ". The report will still be generated using available months."
        )
    else:
        st.success("All 12 months are present.")


def get_sorted_category_rows(summary_df: pd.DataFrame, desired_codes: set) -> pd.DataFrame:
    """Filter and sort rows for a given code set."""
    filtered = summary_df[summary_df["IRS Category Code"].isin(desired_codes)].copy()
    if filtered.empty:
        return filtered
    filtered["__code_int"] = pd.to_numeric(filtered["IRS Category Code"], errors="coerce")
    filtered = filtered.sort_values("__code_int").drop(columns="__code_int")
    return filtered


def build_annual_report(year: int, summary_df: pd.DataFrame, full_df: pd.DataFrame) -> str:
    """Build the complete annual text report."""
    lines = []

    # Header
    lines.append(f"{year} Foreign Area Officer Association Annual Financial Report")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("------------------------------------------------------------------------")
    lines.append("")

    # Revenue (Adjusted)
    lines.append("Revenue Categories (using Adjusted totals)")
    lines.append("")
    lines.append("REVENUE CATEGORIES")
    revenue_summary = get_sorted_category_rows(summary_df, REVENUE_CODES)

    if revenue_summary.empty:
        lines.append("  (No revenue recorded for this period.)")
    else:
        for _, r in revenue_summary.iterrows():
            lines.append(
                f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
                f"{format_currency(r['Adjusted Total Amount'])}"
            )

    lines.append("")

    # Expenses (Adjusted)
    lines.append("Expense Categories (using Adjusted totals)")
    lines.append("")
    lines.append("EXPENSE CATEGORIES")
    expense_summary = get_sorted_category_rows(summary_df, EXPENSE_CODES)

    if expense_summary.empty:
        lines.append("  (No expenses recorded for this period.)")
    else:
        for _, r in expense_summary.iterrows():
            lines.append(
                f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
                f"{format_currency(r['Adjusted Total Amount'])}"
            )

    # ITEMIZED REVENUE
    lines.append("")
    lines.append("Itemized Revenue (BY IRS CATEGORY)")
    lines.append("")
    lines.append("ITEMIZED REVENUE")

    rev_df = full_df[full_df["IRS Category Code"].isin(REVENUE_CODES)].copy()

    if rev_df.empty:
        lines.append("  (No itemized revenue entries.)")
    else:
        # Category 1 – Sponsors
        cat1 = rev_df[rev_df["IRS Category Code"] == "1"].copy()
        if not cat1.empty:
            lines.append(f"  Category 1 – {cat1['IRS Category Label'].iloc[0]}:")

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
                lines.append("    (No sponsor names recorded.)")

        # Other revenue categories
        for code in sorted(REVENUE_CODES - {"1"}, key=lambda x: int(x)):
            cat_df = rev_df[rev_df["IRS Category Code"] == code]
            if cat_df.empty:
                continue

            label = cat_df["IRS Category Label"].iloc[0]
            lines.append(f"  Category {code} – {label}:")
            cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"]).replace("", "UNLABELED")

            group = (
                cat_df.groupby("Itemization Label")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization Label")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # ITEMIZED EXPENSES
    lines.append("")
    lines.append("Itemized Expenses (BY IRS CATEGORY)")
    lines.append("")
    lines.append("ITEMIZED EXPENSES")

    exp_df = full_df[full_df["IRS Category Code"].isin(EXPENSE_CODES)].copy()

    if exp_df.empty:
        lines.append("  (No itemized expense entries.)")
    else:
        # Category 16 – Events
        cat16 = exp_df[exp_df["IRS Category Code"] == "16"].copy()
        if not cat16.empty:
            lines.append(f"  Category 16 – {cat16['IRS Category Label'].iloc[0]} (individual events):")
            lines.append("    Date | Event | Location | Purpose | Amount")
            cat16 = cat16.sort_values(["Date", "Member/Event Label"])

            for _, r in cat16.iterrows():
                lines.append(
                    f"    {r['Date']} | {r['Member/Event Label']} | "
                    f"{r['Event Location']} | {r['Event Purpose']} | "
                    f"{format_currency(r['Amount'])}"
                )

        # Other expense categories
        for code in sorted(EXPENSE_CODES - {"16"}, key=lambda x: int(x)):
            cat_df = exp_df[exp_df["IRS Category Code"] == code]
            if cat_df.empty:
                continue

            if not cat_df["Itemization Label"].str.strip().ne("").any():
                continue

            label = cat_df["IRS Category Label"].iloc[0]
            lines.append(f"  Category {code} – {label} (consolidated by type):")
            cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"]).replace("", "UNLABELED")

            group = (
                cat_df.groupby("Itemization Label")["Amount"]
                .sum()
                .reset_index()
                .sort_values("Itemization Label")
            )
            for _, r in group.iterrows():
                lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    # NEEDS FURTHER INVESTIGATION
    lines.append("")
    lines.append("NEEDS FURTHER INVESTIGATION (Treasurer Flagged)")
    flagged = full_df[full_df["Needs Further Investigation"] == True]

    if flagged.empty:
        lines.append("  (None flagged this period.)")
    else:
        count = len(flagged)
        total = flagged["Amount"].sum()
        lines.append(f"  Count of flagged transactions: {count}")
        lines.append(f"  Net total of flagged amounts: {format_currency(total)}")

    lines.append("")
    lines.append("End of report.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# STEP 1 — Upload CSVs
# ---------------------------------------------------------------------------

st.header("Step 1 – Upload Monthly CSVs")

uploaded_files = st.file_uploader(
    "Upload 1–12 monthly CSVs exported from the FAOA Monthly Treasurer Tool:",
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

# Month coverage check
show_month_coverage(full_df)

# ---------------------------------------------------------------------------
# STEP 2 — Annual Summary
# ---------------------------------------------------------------------------

st.header("Step 2 – Annual Summary by IRS Category")

summary_df = build_summary_table(full_df)

st.write(
    "Review the annual totals below. You may edit the **Adjusted Total Amount** "
    "column to apply year-end corrections. Raw totals come directly from uploaded data."
)

edited_summary_df = st.data_editor(
    summary_df,
    num_rows="fixed",
    disabled=["IRS Category Code", "IRS Category Label", "Raw Total Amount"],
    key="annual_summary_editor",
)

# ---------------------------------------------------------------------------
# STEP 3 — Generate Annual Text Report
# ---------------------------------------------------------------------------

st.header("Step 3 – Generate Annual Text Report")

if "annual_report_text" not in st.session_state:
    st.session_state["annual_report_text"] = ""

if st.button("Generate Annual Text Report"):
    st.session_state["annual_report_text"] = build_annual_report(
        year, edited_summary_df, full_df
    )

if st.session_state["annual_report_text"]:
    st.subheader("Preview – Annual Text Report")
    st.text_area(
        "Report Output (read-only preview)",
        value=st.session_state["annual_report_text"],
        height=500,
    )

    # Downloads
    st.header("Step 4 – Download Outputs")

    st.download_button(
        "Download Annual Text Report (.txt)",
        data=st.session_state["annual_report_text"],
        file_name=f"FAOA_Annual_Financial_Report_{year}.txt",
        mime="text/plain",
    )

    summary_csv = edited_summary_df.to_csv(index=False)
    st.download_button(
        "Download Adjusted Annual Summary (.csv)",
        data=summary_csv,
        file_name=f"FAOA_Annual_Summary_{year}.csv",
        mime="text/csv",
    )
