import io
import json
import requests
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

st.set_page_config(page_title="Carbon MVP Dashboard", layout="wide")

st.title("🌿 Carbon Footprint MVP — Streamlit Dashboard")

# --- Config ---
default_api = "http://127.0.0.1:8000"
api_url = st.sidebar.text_input("FastAPI base URL", value=default_api, help="Where your FastAPI server is running")
org_name = st.sidebar.text_input("Organisation name", value="DemoOrg")

st.sidebar.markdown("---")
st.sidebar.caption("Tips:\n- Start your API first: `uvicorn app.main:app --reload`\n- Seed DB (once): `python -m scripts.init_db`")

# Helper
def api_get(path):
    r = requests.get(f"{api_url}{path}")
    r.raise_for_status()
    return r.json()

def api_post(path, **kwargs):
    r = requests.post(f"{api_url}{path}", **kwargs)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"ok": True}

st.header("1) Upload data")
uploaded = st.file_uploader("Upload activity CSV", type=["csv"], key="csv")
col1, col2 = st.columns(2)
with col1:
    if st.button("Upload to API"):
        if uploaded is None:
            st.error("Please choose a CSV file first.")
        else:
            files = {"file": (uploaded.name, uploaded.getvalue(), "text/csv")}
            params = {"org_name": org_name}
            try:
                res = api_post("/activities/upload_csv", files=files, params=params)
                st.success(f"Ingested {res.get('records_ingested')} records")
                issues = res.get("issues", [])
                if issues:
                    st.warning("Issues:\n- " + "\n- ".join(issues))
            except Exception as e:
                st.exception(e)

with col2:
    if st.button("Run calculation"):
        try:
            res = api_post("/calculate/run")
            st.success("Calculation completed")
        except Exception as e:
            st.exception(e)

st.header("2) Summary results")
try:
    s = api_get("/results/summary")
    st.json(s)
except Exception as e:
    st.info("No results yet. Upload data and run calculation.")
    s = None

if s:
    total = s.get("total_co2e", 0.0)
    colA, colB, colC = st.columns(3)
    colA.metric("Total footprint (kgCO₂e)", f"{total:,.2f}")
    # By scope chart
    by_scope = pd.DataFrame(s.get("by_scope", []))
    if not by_scope.empty:
        st.subheader("By Scope")
        fig1, ax1 = plt.subplots()
        ax1.bar(by_scope["scope"].astype(str), by_scope["co2e"].astype(float))
        ax1.set_xlabel("Scope")
        ax1.set_ylabel("kgCO₂e")
        ax1.set_title("Emissions by Scope")
        st.pyplot(fig1)

    # By category chart
    by_cat = pd.DataFrame(s.get("by_category", []))
    if not by_cat.empty:
        st.subheader("By Category")
        fig2, ax2 = plt.subplots()
        ax2.bar(by_cat["category"].astype(str), by_cat["co2e"].astype(float))
        ax2.set_xlabel("Category")
        ax2.set_ylabel("kgCO₂e")
        ax2.set_title("Emissions by Category")
        plt.setp(ax2.get_xticklabels(), rotation=30, ha="right")
        st.pyplot(fig2)

st.header("3) Factor browser (optional)")
geo = st.text_input("Filter by geography (e.g., GB)", value="")
cat = st.text_input("Filter by category (e.g., electricity)", value="")
if st.button("Load factors"):
    try:
        params = {}
        if cat.strip():
            params["category"] = cat.strip()
        if geo.strip():
            params["geo"] = geo.strip()
        facs = requests.get(f"{api_url}/factors", params=params).json()
        if facs:
            st.dataframe(pd.DataFrame(facs))
        else:
            st.info("No factors found for the filters.")
    except Exception as e:
        st.exception(e)

st.caption("This dashboard is a thin UI over the FastAPI MVP. Replace demo factors with licensed datasets for real use.")
