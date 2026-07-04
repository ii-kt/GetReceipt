from __future__ import annotations

import streamlit as st


DESIGN_SOURCE = "Taste Skill v2 redesign rules"


def inject_design() -> None:
    st.markdown(
        """
        <style>
        :root {
          --gr-bg: #f3f5f7;
          --gr-surface: #ffffff;
          --gr-surface-alt: #eef1f5;
          --gr-ink: #16181d;
          --gr-muted: #66707f;
          --gr-faint: #8b95a5;
          --gr-border: #d8dde5;
          --gr-border-strong: #b8c0cc;
          --gr-accent: #2457ff;
          --gr-accent-soft: #e8edff;
          --gr-success: #0f7d58;
          --gr-warning: #9b6200;
          --gr-danger: #b42318;
          --gr-shadow: 0 18px 42px rgba(18, 25, 38, .09);
          --gr-radius: 6px;
          --gr-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
        }

        .stApp {
          background:
            linear-gradient(90deg, rgba(36, 87, 255, .05) 0 1px, transparent 1px 100%),
            var(--gr-bg);
          color: var(--gr-ink);
        }

        [data-testid="stHeader"] {
          background: rgba(243, 245, 247, .94);
          border-bottom: 1px solid rgba(216, 221, 229, .72);
        }

        .block-container {
          max-width: 1180px;
          padding-top: 1.15rem;
          padding-bottom: 3rem;
        }

        .gr-app-header {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(220px, 300px);
          gap: 1rem;
          align-items: stretch;
          margin-bottom: 1rem;
        }

        .gr-header-main,
        .gr-header-aside {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: var(--gr-shadow);
        }

        .gr-header-main {
          padding: 1.35rem 1.4rem 1.25rem;
          border-left: 6px solid var(--gr-accent);
        }

        .gr-kicker {
          color: var(--gr-accent);
          font-family: var(--gr-mono);
          font-size: .74rem;
          font-weight: 800;
          letter-spacing: .04em;
          text-transform: uppercase;
        }

        .gr-title {
          margin: .35rem 0 .25rem;
          color: var(--gr-ink);
          font-size: clamp(2rem, 4vw, 3.45rem);
          line-height: .96;
          font-weight: 900;
          letter-spacing: 0;
        }

        .gr-subtitle,
        .gr-section-detail {
          color: var(--gr-muted);
          font-size: .94rem;
          line-height: 1.55;
        }

        .gr-header-aside {
          display: grid;
          align-content: stretch;
          padding: 0;
          overflow: hidden;
        }

        .gr-aside-row {
          display: grid;
          grid-template-columns: 1fr auto;
          gap: .75rem;
          align-items: center;
          min-height: 48px;
          padding: .78rem .92rem;
          border-bottom: 1px solid var(--gr-border);
        }

        .gr-aside-row:last-child {
          border-bottom: 0;
        }

        .gr-aside-row span {
          color: var(--gr-muted);
          font-size: .76rem;
          font-weight: 750;
        }

        .gr-aside-row b {
          color: var(--gr-ink);
          font-family: var(--gr-mono);
          font-size: .82rem;
        }

        .gr-section {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(220px, 360px);
          gap: 1rem;
          align-items: end;
          margin: 1.15rem 0 .85rem;
          padding-bottom: .7rem;
          border-bottom: 1px solid var(--gr-border);
        }

        .gr-section-eyebrow {
          color: var(--gr-accent);
          font-family: var(--gr-mono);
          font-size: .72rem;
          font-weight: 800;
          letter-spacing: .04em;
          text-transform: uppercase;
        }

        .gr-section-title {
          margin-top: .15rem;
          color: var(--gr-ink);
          font-size: 1.34rem;
          line-height: 1.25;
          font-weight: 860;
          letter-spacing: 0;
        }

        [data-testid="stMetric"] {
          min-height: 90px;
          padding: .85rem .92rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 10px 24px rgba(18, 25, 38, .06);
        }

        [data-testid="stMetricLabel"] {
          color: var(--gr-muted);
          font-size: .78rem;
          font-weight: 760;
        }

        [data-testid="stMetricValue"] {
          color: var(--gr-ink);
          font-family: var(--gr-mono);
          font-size: 1.72rem;
          font-weight: 850;
        }

        div[data-testid="stTabs"] [role="tablist"] {
          gap: .35rem;
          overflow-x: auto;
          padding: .35rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 10px 24px rgba(18, 25, 38, .05);
        }

        div[data-testid="stTabs"] button[role="tab"] {
          min-height: 38px;
          border-radius: 4px;
          color: var(--gr-muted);
          font-weight: 760;
        }

        div[data-testid="stTabs"] button[aria-selected="true"] {
          color: #ffffff;
          background: var(--gr-accent);
        }

        div[data-testid="stTabs"] button[aria-selected="true"] p {
          color: #ffffff;
        }

        .gr-status-key {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: .55rem;
          margin: .15rem 0 1rem;
        }

        .gr-status-key span {
          display: grid;
          grid-template-columns: 5px minmax(0, 1fr);
          gap: .6rem;
          align-items: center;
          min-height: 38px;
          padding: .42rem .62rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          color: var(--gr-ink);
          font-size: .8rem;
          font-weight: 760;
        }

        .gr-status-key i {
          width: 5px;
          height: 24px;
          border-radius: 2px;
        }

        .gr-status-key .is-open i { background: var(--gr-accent); }
        .gr-status-key .is-done i { background: var(--gr-success); }
        .gr-status-key .is-none i { background: var(--gr-warning); }

        .gr-month-cell {
          display: flex;
          min-height: 42px;
          align-items: center;
          color: var(--gr-ink);
          font-size: .86rem;
          font-weight: 800;
        }

        .gr-month-band {
          margin: .95rem 0 .45rem;
          padding: .55rem .72rem;
          border: 1px solid var(--gr-border);
          border-left: 5px solid var(--gr-accent);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          color: var(--gr-ink);
          font-size: .92rem;
          font-weight: 850;
        }

        .gr-service-cell {
          display: grid;
          align-content: center;
          min-height: 40px;
          padding: .18rem 0 .48rem;
        }

        .gr-service-cell span {
          color: var(--gr-ink);
          font-size: .9rem;
          font-weight: 820;
          line-height: 1.25;
        }

        .gr-service-cell small {
          margin-top: .12rem;
          color: var(--gr-muted);
          font-size: .7rem;
          font-weight: 700;
        }

        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stLinkButton"] > a {
          min-height: 40px;
          border-radius: var(--gr-radius);
          border: 1px solid var(--gr-border-strong);
          background: var(--gr-surface);
          color: var(--gr-ink);
          font-weight: 760;
          box-shadow: none;
        }

        .stButton > button[kind="primary"],
        .stDownloadButton > button[kind="primary"] {
          border-color: var(--gr-accent);
          background: var(--gr-accent);
          color: #ffffff;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover,
        [data-testid="stLinkButton"] > a:hover {
          border-color: var(--gr-accent);
          color: var(--gr-accent);
          box-shadow: 0 0 0 3px rgba(36, 87, 255, .12);
        }

        .stButton > button[kind="primary"]:hover,
        .stDownloadButton > button[kind="primary"]:hover {
          color: #ffffff;
          background: #1746e6;
        }

        div[data-testid="stExpander"] {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 8px 18px rgba(18, 25, 38, .05);
        }

        div[data-testid="stDataFrame"] {
          overflow: hidden;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
        }

        [data-testid="stAlert"] {
          border-radius: var(--gr-radius);
          border: 1px solid var(--gr-border);
          background: var(--gr-surface);
        }

        code {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface-alt);
          color: var(--gr-ink);
        }

        input, textarea, select {
          border-radius: var(--gr-radius) !important;
        }

        hr {
          border-color: var(--gr-border);
        }

        @media (max-width: 760px) {
          .block-container {
            padding-left: .9rem;
            padding-right: .9rem;
          }

          .gr-app-header,
          .gr-section {
            grid-template-columns: 1fr;
          }

          .gr-status-key {
            grid-template-columns: 1fr;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_app_header(*, saved_count: int, open_slots: int, done_slots: int, current_month: str) -> None:
    st.markdown(
        f"""
        <div class="gr-app-header">
          <div class="gr-header-main">
            <div class="gr-kicker">GetReceipt</div>
            <div class="gr-title">Receipt operations</div>
            <div class="gr-subtitle">Drive保存、取得状況、台帳監査を一画面で処理します。</div>
          </div>
          <div class="gr-header-aside">
            <div class="gr-aside-row"><span>保存済ファイル</span><b>{saved_count}</b></div>
            <div class="gr-aside-row"><span>未取得枠</span><b>{open_slots}</b></div>
            <div class="gr-aside-row"><span>保管完了枠</span><b>{done_slots}</b></div>
            <div class="gr-aside-row"><span>現在の対象</span><b>{current_month}</b></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_heading(eyebrow: str, title: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="gr-section">
          <div>
            <div class="gr-section-eyebrow">{eyebrow}</div>
            <div class="gr-section-title">{title}</div>
          </div>
          <div class="gr-section-detail">{detail}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
