from __future__ import annotations

import streamlit as st


DESIGN_SOURCE = "shadcn/ui skill + Material UI theming skill + Magic UI visual direction"


def inject_design() -> None:
    st.markdown(
        """
        <style>
        :root {
          --gr-bg: #0e0f14;
          --gr-panel: rgba(22, 24, 34, .86);
          --gr-panel-solid: #171a25;
          --gr-ink: #f8fbff;
          --gr-muted: #aeb8c7;
          --gr-rule: rgba(255, 255, 255, .14);
          --gr-cyan: #38d5ff;
          --gr-green: #00e0a4;
          --gr-pink: #ff4fd8;
          --gr-amber: #ffb020;
          --gr-red: #ff5d5d;
          --gr-lime: #c9ff4f;
          --gr-shadow: rgba(0, 224, 164, .18);
        }

        .stApp {
          background:
            linear-gradient(135deg, rgba(56, 213, 255, .10), transparent 28%),
            linear-gradient(315deg, rgba(255, 79, 216, .11), transparent 32%),
            repeating-linear-gradient(90deg, rgba(255,255,255,.035) 0 1px, transparent 1px 46px),
            repeating-linear-gradient(0deg, rgba(255,255,255,.026) 0 1px, transparent 1px 46px),
            var(--gr-bg);
          color: var(--gr-ink);
        }

        [data-testid="stHeader"] {
          background: transparent;
        }

        .block-container {
          max-width: 1240px;
          padding-top: 1rem;
          padding-bottom: 3rem;
        }

        .gr-app-header {
          position: relative;
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 1.25rem;
          align-items: end;
          padding: 1.35rem 1.4rem 1.25rem;
          border: 1px solid var(--gr-rule);
          border-radius: 8px;
          margin-bottom: 1rem;
          background:
            linear-gradient(90deg, rgba(0, 224, 164, .20), rgba(56, 213, 255, .10) 44%, rgba(255, 79, 216, .18)),
            var(--gr-panel);
          box-shadow: 0 18px 55px rgba(0, 0, 0, .34), inset 0 1px 0 rgba(255,255,255,.18);
          overflow: hidden;
        }

        .gr-app-header:after {
          content: "";
          position: absolute;
          left: 1.4rem;
          right: 0;
          bottom: 0;
          height: 4px;
          background: linear-gradient(90deg, var(--gr-green), var(--gr-cyan), var(--gr-pink), var(--gr-amber));
        }

        .gr-kicker {
          display: inline-flex;
          align-items: center;
          gap: .5rem;
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
          font-size: .74rem;
          font-weight: 800;
          letter-spacing: .08em;
          color: var(--gr-lime);
          text-transform: uppercase;
        }

        .gr-kicker:before {
          content: "";
          width: .62rem;
          height: .62rem;
          background: var(--gr-green);
          box-shadow: 0 0 18px var(--gr-green);
          border-radius: 50%;
        }

        .gr-title {
          margin: .35rem 0 .2rem;
          font-family: "Arial Black", "Yu Gothic", "Hiragino Sans", sans-serif;
          font-size: 2.55rem;
          line-height: 1.02;
          font-weight: 900;
          text-transform: uppercase;
          text-shadow: 0 0 28px rgba(56, 213, 255, .35);
        }

        .gr-subtitle,
        .gr-section-detail {
          color: var(--gr-muted);
          font-size: .93rem;
          line-height: 1.65;
        }

        .gr-header-aside {
          min-width: 190px;
          padding: .85rem;
          border: 1px solid rgba(255, 255, 255, .18);
          border-radius: 8px;
          background: rgba(8, 10, 18, .46);
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
        }

        .gr-header-aside b {
          display: block;
          color: var(--gr-green);
          font-size: .82rem;
          margin-bottom: .45rem;
        }

        .gr-header-aside span {
          display: block;
          color: var(--gr-muted);
          font-size: .75rem;
          line-height: 1.55;
        }

        .gr-section {
          display: flex;
          align-items: flex-end;
          justify-content: space-between;
          gap: 1rem;
          padding: 1rem 0 .65rem;
          border-bottom: 1px solid var(--gr-rule);
          margin: .95rem 0 1rem;
        }

        .gr-section-eyebrow {
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
          font-size: .72rem;
          font-weight: 850;
          color: var(--gr-cyan);
          letter-spacing: .08em;
          text-transform: uppercase;
        }

        .gr-section-title {
          margin: .18rem 0 0;
          font-size: 1.38rem;
          line-height: 1.25;
          font-weight: 900;
        }

        [data-testid="stMetric"] {
          background:
            linear-gradient(180deg, rgba(255, 255, 255, .075), rgba(255, 255, 255, .025)),
            var(--gr-panel-solid);
          border: 1px solid var(--gr-rule);
          border-radius: 8px;
          padding: .85rem .95rem;
          min-height: 94px;
          box-shadow: 0 14px 34px rgba(0, 0, 0, .24);
        }

        [data-testid="stMetricLabel"] {
          color: var(--gr-muted);
          font-size: .8rem;
        }

        [data-testid="stMetricValue"] {
          color: var(--gr-ink);
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
          text-shadow: 0 0 18px rgba(56, 213, 255, .24);
        }

        div[data-testid="stTabs"] [role="tablist"] {
          gap: .5rem;
          overflow-x: auto;
          border-bottom: 1px solid rgba(255, 255, 255, .12);
          padding-bottom: .45rem;
        }

        div[data-testid="stTabs"] button[role="tab"] {
          border-radius: 8px;
          border: 1px solid var(--gr-rule);
          background: rgba(255,255,255,.05);
          color: var(--gr-muted);
          min-height: 36px;
        }

        div[data-testid="stTabs"] button[aria-selected="true"] {
          color: var(--gr-ink);
          background: linear-gradient(90deg, rgba(0,224,164,.25), rgba(56,213,255,.18));
          border-color: rgba(56,213,255,.44);
        }

        .gr-status-key {
          display: flex;
          flex-wrap: wrap;
          gap: .55rem;
          margin: .2rem 0 1rem;
        }

        .gr-status-key span {
          display: inline-flex;
          align-items: center;
          gap: .45rem;
          min-height: 2.1rem;
          padding: .25rem .65rem;
          border: 1px solid var(--gr-rule);
          border-radius: 8px;
          background: rgba(255, 255, 255, .06);
          font-size: .78rem;
          font-weight: 800;
          color: var(--gr-ink);
        }

        .gr-status-key i {
          display: inline-block;
          width: .62rem;
          height: .62rem;
          border-radius: 50%;
        }

        .gr-status-key .is-open i { background: var(--gr-cyan); box-shadow: 0 0 14px var(--gr-cyan); }
        .gr-status-key .is-done i { background: var(--gr-green); }
        .gr-status-key .is-none i { background: var(--gr-amber); }

        .gr-month-cell {
          display: flex;
          min-height: 44px;
          align-items: center;
          font-size: .86rem;
          font-weight: 800;
        }

        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stLinkButton"] > a {
          min-height: 42px;
          border-radius: 8px;
          font-weight: 750;
          border-color: rgba(255, 255, 255, .18);
          box-shadow: 0 12px 22px rgba(0,0,0,.18);
        }

        .stButton > button[kind="primary"],
        .stDownloadButton > button[kind="primary"] {
          background: linear-gradient(90deg, var(--gr-green), var(--gr-cyan));
          border-color: rgba(56, 213, 255, .55);
          color: #061014;
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover,
        [data-testid="stLinkButton"] > a:hover {
          transform: translateY(-1px);
          box-shadow: 0 16px 30px rgba(0,0,0,.24), 0 0 24px var(--gr-shadow);
        }

        div[data-testid="stExpander"] {
          border-color: var(--gr-rule);
          border-radius: 8px;
          background: rgba(22, 24, 34, .72);
        }

        div[data-testid="stDataFrame"] {
          border: 1px solid var(--gr-rule);
          border-radius: 8px;
          overflow: hidden;
        }

        code {
          border-radius: 8px;
          border: 1px solid rgba(56, 213, 255, .20);
        }

        [data-testid="stAlert"] {
          border-radius: 8px;
          border: 1px solid rgba(255, 176, 32, .36);
        }

        input, textarea, select {
          border-radius: 8px !important;
        }

        @media (prefers-reduced-motion: no-preference) {
          .gr-app-header {
            animation: gr-pop .45s ease-out both;
          }

          @keyframes gr-pop {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
          }
        }

        @media (max-width: 720px) {
          .block-container {
            padding-left: 1rem;
            padding-right: 1rem;
          }

          .gr-title {
            font-size: 1.9rem;
          }

          .gr-section {
            display: block;
          }

          .gr-app-header {
            grid-template-columns: 1fr;
            padding: 1.1rem;
          }

          .gr-header-aside {
            min-width: 0;
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
          <div>
            <div class="gr-kicker">GETRECEIPT CONTROL</div>
            <div class="gr-title">Receipt Command</div>
            <div class="gr-subtitle">家賃、通信、電気、携帯の月次アーカイブ。</div>
          </div>
          <div class="gr-header-aside">
            <b>ACTIVE LEDGER</b>
            <span>OPEN SLOTS {open_slots}</span>
            <span>SAVED FILES {saved_count}</span>
            <span>{current_month}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(4)
    cols[0].metric("保存済ファイル", saved_count)
    cols[1].metric("未取得枠", open_slots)
    cols[2].metric("保管完了枠", done_slots)
    cols[3].metric("現在の対象", current_month)


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
