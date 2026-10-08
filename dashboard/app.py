"""M1：儀表板骨架（深色主題）。

M1 只做「資料概況」頁：資料血緣、SECOM 標籤分布、缺失率分布。
M3 起才會加入 commonality 排名，M7/M8 加入預測面板。

啟動：docker compose up -d dashboard → http://localhost:8501
"""
from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Commonality Analysis", page_icon="🏭", layout="wide")

# 深色主題 + 台灣慣例：不良/異常用紅、正常用綠
BG, PANEL, TXT, GRID = "#0B1220", "#111C2E", "#E5E7EB", "#26344A"
RED, GREEN, ACCENT, AMBER = "#F87171", "#34D399", "#38BDF8", "#FBBF24"

PLOT_LAYOUT = dict(
    template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
    font=dict(color=TXT, size=14),
    xaxis=dict(gridcolor=GRID, zerolinecolor=GRID),
    yaxis=dict(gridcolor=GRID, zerolinecolor=GRID),
    margin=dict(l=40, r=20, t=50, b=40),
)

st.markdown(
    f"""<style>
    .stApp {{ background-color: {BG}; }}
    .block-container {{ padding-top: 2rem; }}
    h1, h2, h3 {{ color: {TXT}; }}
    [data-testid="stMetricValue"] {{ color: {ACCENT}; }}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def conn():
    import psycopg
    return psycopg.connect(
        host=os.environ.get("PGHOST", "db"), port=int(os.environ.get("PGPORT", 5432)),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"], autocommit=True,
    )


def q(sql: str) -> pd.DataFrame:
    with conn().cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


st.title("🏭 Commonality Analysis — 資料概況")
st.caption("M1：環境建置與資料取得。後續里程碑將加入共同性分析（M3）、FDC（M5）與預測（M7–M8）。")

try:
    cat = q("SELECT file_key, bytes, sha256, downloaded_at, row_count, description FROM raw.data_catalog ORDER BY file_key")
    dist = q("SELECT label_name, n, pct FROM qa.secom_label_distribution")
    prof = q("SELECT feature_id, missing_rate FROM qa.secom_column_profile ORDER BY missing_rate DESC")
except Exception as e:  # noqa: BLE001
    st.error(f"無法連線資料庫或資料尚未載入：{e}\n\n請先執行 `docker compose run --rm etl python download_data.py` "
             f"與 `docker compose run --rm etl python load_secom.py`。")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("已建檔資料檔", f"{len(cat)}")
c2.metric("SECOM 樣本數", f"{int(dist['n'].sum()):,}")
fail_pct = float(dist.loc[dist["label_name"] == "fail", "pct"].iloc[0]) if len(dist) else 0.0
c3.metric("SECOM 失敗率", f"{fail_pct:.2f}%")
c4.metric("SECOM 欄位數", f"{len(prof)}")

left, right = st.columns(2)
with left:
    st.subheader("SECOM 標籤分布")
    fig = go.Figure(go.Bar(
        x=dist["label_name"], y=dist["n"],
        marker_color=[GREEN if n == "pass" else RED for n in dist["label_name"]],
        text=dist["n"], textposition="outside",
    ))
    fig.update_layout(**PLOT_LAYOUT, height=340, title="pass = 良品、fail = 不良")
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("每欄缺失率分布")
    fig = go.Figure(go.Histogram(x=prof["missing_rate"] * 100, nbinsx=25, marker_color=ACCENT))
    fig.update_layout(**PLOT_LAYOUT, height=340, title="缺失率（%）→ 缺失值本身就是資訊，不可直接填平均")
    st.plotly_chart(fig, use_container_width=True)

st.subheader("資料血緣（Data Lineage）")
st.dataframe(cat, use_container_width=True, hide_index=True)

with st.expander("缺失率最高的 15 欄"):
    st.dataframe(prof.head(15), use_container_width=True, hide_index=True)
