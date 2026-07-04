from __future__ import annotations

import streamlit as st


DESIGN_SOURCE = "Mobbin, Land-book, Awwwards, and Recent app-gallery direction"


def inject_design() -> None:
    st.markdown(
        """
        <style>
        :root {
          --gr-bg: #f6f8f5;
          --gr-surface: #ffffff;
          --gr-surface-soft: #eef5ef;
          --gr-ink: #111411;
          --gr-muted: #657064;
          --gr-faint: #98a195;
          --gr-border: #dce5dc;
          --gr-border-strong: #b8c7b9;
          --gr-accent: #29d982;
          --gr-accent-deep: #0b7d48;
          --gr-accent-soft: #ddf8e9;
          --gr-coral: #ff6b4a;
          --gr-warning: #b7791f;
          --gr-danger: #c7372f;
          --gr-shadow: 0 14px 34px rgba(17, 20, 17, .08);
          --gr-radius: 8px;
          --gr-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
          --gr-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Yu Gothic", "Hiragino Kaku Gothic ProN", sans-serif;
        }

        .stApp {
          background: var(--gr-bg);
          color: var(--gr-ink);
          font-family: var(--gr-sans);
        }

        #MainMenu,
        footer,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"] {
          display: none;
        }

        [data-testid="stHeader"] {
          background: rgba(246, 248, 245, .96);
          border-bottom: 1px solid rgba(220, 229, 220, .88);
        }

        .block-container {
          max-width: 1040px;
          padding: 1.05rem 1rem 3.2rem;
        }

        .gr-app-header {
          display: grid;
          gap: 1rem;
          margin: .25rem 0 1.05rem;
          padding: .35rem 0 1.05rem;
          border-bottom: 1px solid var(--gr-border);
        }

        .gr-header-main {
          display: grid;
          gap: .75rem;
        }

        .gr-brand-row {
          display: flex;
          gap: .85rem;
          align-items: flex-start;
          justify-content: space-between;
        }

        .gr-brand-lockup {
          display: flex;
          gap: .78rem;
          align-items: center;
          min-width: 0;
        }

        .gr-brand-mark {
          display: grid;
          width: 42px;
          height: 42px;
          flex: 0 0 42px;
          place-items: center;
          border-radius: var(--gr-radius);
          background: var(--gr-ink);
          color: #ffffff;
          font-size: 1rem;
          font-weight: 900;
          letter-spacing: 0;
          box-shadow: 0 10px 24px rgba(17, 20, 17, .16);
        }

        .gr-kicker {
          color: var(--gr-muted);
          font-size: .76rem;
          font-weight: 760;
          letter-spacing: 0;
        }

        .gr-title {
          margin-top: .08rem;
          color: var(--gr-ink);
          font-size: 2.65rem;
          line-height: .96;
          font-weight: 880;
          letter-spacing: 0;
        }

        .gr-live-badge {
          flex: 0 0 auto;
          padding: .5rem .68rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          color: var(--gr-muted);
          font-size: .76rem;
          font-weight: 780;
          box-shadow: 0 8px 18px rgba(17, 20, 17, .05);
        }

        .gr-live-badge i {
          display: inline-block;
          width: .48rem;
          height: .48rem;
          margin-right: .38rem;
          border-radius: 50%;
          background: var(--gr-accent);
          box-shadow: 0 0 0 4px rgba(41, 217, 130, .16);
          vertical-align: .05rem;
        }

        .gr-subtitle,
        .gr-section-detail {
          color: var(--gr-muted);
          font-size: .95rem;
          line-height: 1.58;
        }

        .gr-header-aside {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: .62rem;
        }

        .gr-aside-row {
          display: grid;
          gap: .3rem;
          min-height: 74px;
          align-content: center;
          padding: .78rem .84rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: rgba(255, 255, 255, .9);
          box-shadow: 0 10px 22px rgba(17, 20, 17, .05);
        }

        .gr-aside-row span {
          color: var(--gr-muted);
          font-size: .74rem;
          font-weight: 760;
          letter-spacing: 0;
        }

        .gr-aside-row b {
          overflow: hidden;
          color: var(--gr-ink);
          font-family: var(--gr-mono);
          font-size: 1rem;
          font-weight: 850;
          line-height: 1.2;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .gr-section {
          display: grid;
          grid-template-columns: minmax(0, 1fr) minmax(180px, 330px);
          gap: 1rem;
          align-items: end;
          margin: 1.25rem 0 .9rem;
          padding-bottom: .72rem;
          border-bottom: 1px solid var(--gr-border);
        }

        .gr-section-eyebrow {
          display: inline-flex;
          width: fit-content;
          margin-bottom: .28rem;
          padding: .22rem .46rem;
          border-radius: 999px;
          background: var(--gr-accent-soft);
          color: var(--gr-accent-deep);
          font-size: .72rem;
          font-weight: 820;
          letter-spacing: 0;
        }

        .gr-section-title {
          color: var(--gr-ink);
          font-size: 1.5rem;
          line-height: 1.2;
          font-weight: 880;
          letter-spacing: 0;
        }

        div[data-testid="stTabs"] [role="tablist"] {
          position: sticky;
          top: .35rem;
          z-index: 5;
          gap: .28rem;
          overflow-x: auto;
          padding: .32rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: rgba(255, 255, 255, .92);
          box-shadow: 0 12px 28px rgba(17, 20, 17, .08);
          backdrop-filter: blur(18px);
        }

        div[data-testid="stTabs"] button[role="tab"] {
          min-height: 38px;
          border-radius: 6px;
          color: var(--gr-muted);
          font-weight: 780;
          letter-spacing: 0;
        }

        div[data-testid="stTabs"] button[aria-selected="true"] {
          color: #ffffff;
          background: var(--gr-ink);
        }

        div[data-testid="stTabs"] button[aria-selected="true"] p {
          color: #ffffff;
        }

        .gr-status-key {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: .62rem;
          margin: .25rem 0 1.05rem;
        }

        .gr-status-key span {
          display: grid;
          grid-template-columns: 9px minmax(0, 1fr);
          gap: .62rem;
          align-items: center;
          min-height: 42px;
          padding: .48rem .66rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          color: var(--gr-ink);
          font-size: .82rem;
          font-weight: 780;
          box-shadow: 0 8px 18px rgba(17, 20, 17, .04);
        }

        .gr-status-key i {
          width: 9px;
          height: 28px;
          border-radius: 999px;
        }

        .gr-status-key .is-open i { background: var(--gr-accent); }
        .gr-status-key .is-done i { background: var(--gr-ink); }
        .gr-status-key .is-none i { background: var(--gr-coral); }

        .gr-month-band {
          margin: 1.05rem 0 .52rem;
          padding: .72rem .82rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-ink);
          color: #ffffff;
          font-size: .98rem;
          font-weight: 850;
          letter-spacing: 0;
          box-shadow: 0 12px 26px rgba(17, 20, 17, .1);
        }

        .gr-service-cell {
          display: grid;
          align-content: center;
          min-height: 42px;
          padding: .18rem 0 .52rem;
        }

        .gr-service-cell span {
          color: var(--gr-ink);
          font-size: .95rem;
          font-weight: 820;
          line-height: 1.25;
        }

        .gr-service-cell small {
          margin-top: .12rem;
          color: var(--gr-muted);
          font-size: .72rem;
          font-weight: 700;
        }

        [data-testid="stMetric"] {
          min-height: 92px;
          padding: .88rem .92rem;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 10px 22px rgba(17, 20, 17, .05);
        }

        [data-testid="stMetricLabel"] {
          color: var(--gr-muted);
          font-size: .78rem;
          font-weight: 780;
        }

        [data-testid="stMetricValue"] {
          color: var(--gr-ink);
          font-family: var(--gr-mono);
          font-size: 1.74rem;
          font-weight: 850;
        }

        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stLinkButton"] > a {
          min-height: 42px;
          border: 1px solid var(--gr-border-strong);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          color: var(--gr-ink);
          font-weight: 800;
          letter-spacing: 0;
          box-shadow: 0 8px 18px rgba(17, 20, 17, .04);
          transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease, background .15s ease;
        }

        .stButton > button[kind="primary"],
        .stDownloadButton > button[kind="primary"],
        button[data-testid="baseButton-primary"] {
          border-color: #0f8a50;
          background: var(--gr-accent);
          color: var(--gr-ink);
        }

        .stButton > button:hover,
        .stDownloadButton > button:hover,
        [data-testid="stLinkButton"] > a:hover {
          border-color: var(--gr-ink);
          color: var(--gr-ink);
          box-shadow: 0 0 0 4px rgba(41, 217, 130, .16);
          transform: translateY(-1px);
        }

        .stButton > button[kind="primary"]:hover,
        .stDownloadButton > button[kind="primary"]:hover,
        button[data-testid="baseButton-primary"]:hover {
          background: #35e58b;
          color: var(--gr-ink);
        }

        .stButton > button:disabled {
          border-color: var(--gr-border);
          background: #eef1ec;
          color: var(--gr-faint);
          opacity: 1;
          transform: none;
          box-shadow: none;
        }

        div[data-testid="stExpander"] {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 8px 18px rgba(17, 20, 17, .05);
        }

        div[data-testid="stDataFrame"] {
          overflow: hidden;
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
          box-shadow: 0 10px 22px rgba(17, 20, 17, .04);
        }

        [data-testid="stAlert"] {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface);
        }

        code {
          border: 1px solid var(--gr-border);
          border-radius: var(--gr-radius);
          background: var(--gr-surface-soft);
          color: var(--gr-ink);
        }

        input,
        textarea,
        select {
          border-radius: var(--gr-radius) !important;
        }

        hr {
          border-color: var(--gr-border);
        }

        @media (max-width: 760px) {
          .block-container {
            padding: .82rem .86rem 3rem;
          }

          .gr-brand-row {
            align-items: center;
          }

          .gr-brand-mark {
            width: 38px;
            height: 38px;
            flex-basis: 38px;
          }

          .gr-title {
            font-size: 2.1rem;
          }

          .gr-live-badge {
            padding: .42rem .54rem;
            font-size: .72rem;
          }

          .gr-header-aside {
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: .5rem;
          }

          .gr-aside-row {
            min-height: 68px;
            padding: .68rem .72rem;
          }

          .gr-section {
            grid-template-columns: 1fr;
            gap: .35rem;
            margin-top: 1.05rem;
          }

          .gr-status-key {
            grid-template-columns: 1fr;
          }

          .gr-service-cell small {
            display: none;
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
            <div class="gr-brand-row">
              <div class="gr-brand-lockup">
                <div class="gr-brand-mark">GR</div>
                <div>
                  <div class="gr-kicker">Drive receipt sync</div>
                  <div class="gr-title">GetReceipt</div>
                </div>
              </div>
              <div class="gr-live-badge"><i></i>Cloud</div>
            </div>
            <div class="gr-subtitle">領収書の取得状況、Drive保存、台帳整理をまとめて確認します。</div>
          </div>
          <div class="gr-header-aside">
            <div class="gr-aside-row"><span>保存済み</span><b>{saved_count}</b></div>
            <div class="gr-aside-row"><span>未取得</span><b>{open_slots}</b></div>
            <div class="gr-aside-row"><span>完了</span><b>{done_slots}</b></div>
            <div class="gr-aside-row"><span>対象月</span><b>{current_month}</b></div>
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
