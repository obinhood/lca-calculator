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
api_key = st.sidebar.text_input("API key", type="password",
                                help="From POST /organisations (shown once at registration)")

with st.sidebar.expander("Register a new organisation"):
    new_org = st.text_input("Organisation name", value="DemoOrg", key="reg_name")
    if st.button("Register"):
        try:
            r = requests.post(f"{api_url}/organisations", params={"name": new_org})
            r.raise_for_status()
            st.code(r.json()["api_key"])
            st.caption("Copy this key into the API key box — it is shown only once.")
        except Exception as e:
            st.error(str(e))

st.sidebar.markdown("---")
st.sidebar.caption("Tips:\n- Start your API first: `uvicorn app.main:app --reload`\n- Init DB (once): `python -m scripts.init_db`")

def _headers():
    return {"X-API-Key": api_key} if api_key else {}

# Helper
def api_get(path, params=None):
    r = requests.get(f"{api_url}{path}", params=params, headers=_headers())
    r.raise_for_status()
    return r.json()

def api_post(path, **kwargs):
    r = requests.post(f"{api_url}{path}", headers=_headers(), **kwargs)
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
            try:
                res = api_post("/activities/upload_csv", files=files)
                mapping = res.get("mapping", {})
                st.success(f"Ingested {res.get('records_ingested')} records — "
                           f"{mapping.get('auto', 0)} auto-mapped, "
                           f"{mapping.get('needs_review', 0)} need review, "
                           f"{mapping.get('unmapped', 0)} unmapped")
                issues = res.get("issues", [])
                if issues:
                    st.warning("Issues:\n- " + "\n- ".join(issues))
            except Exception as e:
                st.exception(e)

with col2:
    if st.button("Run calculation"):
        try:
            res = api_post("/calculate/run")
            cov = (res or {}).get("coverage") or {}
            st.success(f"Calculation completed — {cov.get('coverage_pct', '?')}% coverage")
            if res.get("partial"):
                st.warning(f"PARTIAL RUN — excluded: {res.get('partial_reasons')}")
            if cov.get("warning"):
                st.warning(cov["warning"])
        except Exception as e:
            st.exception(e)

st.header("1b) Mapping review queue")
try:
    queue = api_get("/mappings/review")
    if queue:
        st.warning(f"{len(queue)} activities need a mapping decision before they enter totals.")
        for item in queue:
            sf = item.get("suggested_factor") or {}
            cols = st.columns([4, 1])
            cols[0].write(f"**#{item['activity_id']}** {item['date']} — {item['category']}"
                          f"/{item['subcategory'] or '—'} {item['quantity']} {item['unit']} ({item['geo']}) → "
                          f"suggests {sf.get('category')}/{sf.get('subcategory') or '—'} "
                          f"[{sf.get('geography')}] {sf.get('value')} kgCO₂e/{sf.get('unit')} "
                          f"(basis: {item['mapping_basis']}, confidence {item['mapping_confidence']})")
            if cols[1].button("Approve", key=f"appr_{item['activity_id']}"):
                api_post(f"/mappings/{item['activity_id']}/approve")
                st.rerun()
    else:
        st.caption("Review queue is empty.")
except Exception:
    st.caption("Review queue unavailable (check API key).")

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
