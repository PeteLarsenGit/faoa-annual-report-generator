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
    "an annual roll-up, adjust IRS category totals, and produce a formatted annual text report."
)

# ---------------------------------------------------------------------------
# Constants / Canonical Labels
# ---------------------------------------------------------------------------

REVENUE_CODES = {"1", "2", "3", "4", "6", "7", "9"}
EXPENSE_CODES = {"14", "15", "16", "18", "19", "22", "23"}
ALL_CODES = REVENUE_CODES | EXPENSE_CODES

CATEGORY_LABELS = {
    "1": "Gifts, grants, contributions received",
    "2": "Membership fees received",
    "3": "Gross sales of inventory",
    "4": "Other revenue",
    "6": "Investment income",
    "7": "Other income",
    "9": "Gross receipts from activities related to exempt purpose",
    "14": "Professional fees and other payments to independent contractors",
    "15": "Occupancy, rent, utilities, and maintenance",
    "16": "Disbursements to/for members",
    "18": "Office expenses",
    "19": "Travel",
    "22": "Payments to affiliates",
    "23": "Other expenses",
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
    "and payment processing."
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
        df[col] = clean_str_series(df[col])

    for col in ["Potential Sponsorship", "Needs Further Investigation"]:
        df[col] = coerce_bool_series(df[col])

    return df


def validate_year(df: pd.DataFrame) -> int:
    years = df["Year"].unique()
    if len(years) != 1:
        st.error(f"All files must be from one year. Found: {sorted(years)}")
        st.stop()
    return int(years[0])


def validate_categories(df: pd.DataFrame):
    unknown = set(df["IRS Category Code"]) - ALL_CODES
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
    summary["__sort"] = summary["IRS Category Code"].astype(int)
    return summary.sort_values("__sort").drop(columns="__sort")


def build_annual_report(year, summary_df, full_df, gala_ticket_amount):
    lines = []

    lines.append(f"{year} Foreign Area Officer Association Annual Financial Report")
    lines.append("Foreign Area Officer Association (FAOA)")
    lines.append("------------------------------------------------------------")
    lines.append("")

    lines.append("REVENUE CATEGORIES")
    for _, r in summary_df[summary_df["IRS Category Code"].isin(REVENUE_CODES)].iterrows():
        lines.append(
            f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
            f"{format_currency(r['Adjusted Total Amount'])}"
        )

    lines.append("")
    lines.append("EXPENSE CATEGORIES")
    for _, r in summary_df[summary_df["IRS Category Code"].isin(EXPENSE_CODES)].iterrows():
        lines.append(
            f"  {r['IRS Category Code']} - {r['IRS Category Label']}: "
            f"{format_currency(r['Adjusted Total Amount'])}"
        )

    lines.append("")
    lines.append("ITEMIZED EXPENSES")
    lines.append("")
    lines.append(PROFESSIONAL_FEES_EXPLANATION)
    lines.append("")

    exp_df = full_df[full_df["IRS Category Code"].isin(EXPENSE_CODES)]

    for code in sorted(EXPENSE_CODES, key=int):
        cat_df = exp_df[exp_df["IRS Category Code"] == code]
        if cat_df.empty:
            continue

        label = cat_df["IRS Category Label"].iloc[0]
        lines.append(f"  Category {code} – {label}:")

        cat_df["Itemization Label"] = clean_str_series(cat_df["Itemization Label"]).replace("", "UNLABELED")
        grouped = cat_df.groupby("Itemization Label")["Amount"].sum().reset_index()

        for _, r in grouped.iterrows():
            lines.append(f"    {r['Itemization Label']}: {format_currency(r['Amount'])}")

    lines.append("")
    lines.append("End of report.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Upload + Generate
# ---------------------------------------------------------------------------

uploaded_files = st.file_uploader(
    "Upload 1–12 monthly CSVs",
    type=["csv"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.stop()

dfs = [ensure_columns(pd.read_csv(f)) for f in uploaded_files]
full_df = pd.concat(dfs, ignore_index=True)

year = validate_year(full_df)
validate_categories(full_df)

summary_df = build_summary_table(full_df)

if st.button("Generate Annual Report"):
    report = build_annual_report(year, summary_df, full_df, 0.0)
    st.text_area("Annual Report", report, height=500)
    st.download_button(
        "Download Report",
        report,
        f"FAOA_Annual_Financial_Report_{year}.txt",
        "text/plain",
    )
