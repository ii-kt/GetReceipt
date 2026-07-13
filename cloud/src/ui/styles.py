from __future__ import annotations

from html import escape

import streamlit as st


_STATUS = {
    "saved": ("Drive確認済み", "check"),
    "missing": ("未取得", "missing"),
    "queued": ("待機中", "queued"),
    "running": ("取得中", "running"),
    "failed": ("失敗", "failed"),
    "not_run": ("未実行", "queued"),
}


def inject_design() -> None:
    st.markdown(
        """
        <style>
        :root {
          --gr-ink: #171914;
          --gr-muted: #6e7169;
          --gr-canvas: #f3f1eb;
          --gr-card: #fffefa;
          --gr-line: #dedbd1;
          --gr-success: #167447;
          --gr-success-soft: #e5f2e9;
          --gr-danger: #b83a32;
          --gr-danger-soft: #f9e9e6;
          --gr-progress: #2d63d7;
          --gr-progress-soft: #e8eefc;
          --gr-radius: 20px;
        }

        html, body, [class*="css"], .stApp {
          font-family: Inter, "Noto Sans JP", "Yu Gothic UI", sans-serif;
        }

        .stApp {
          color: var(--gr-ink);
          background:
            radial-gradient(circle at 50% -140px, rgba(255,255,255,.98) 0, rgba(255,255,255,0) 390px),
            var(--gr-canvas);
        }

        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        #MainMenu,
        footer {
          display: none !important;
        }

        [data-testid="stAppViewContainer"] > .main {
          overflow: visible;
        }

        .block-container {
          width: min(100%, 560px) !important;
          max-width: 560px !important;
          padding: 28px 22px 56px !important;
        }

        .gr-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 16px;
          margin: 0 0 22px;
        }

        .gr-brand {
          display: flex;
          align-items: center;
          gap: 11px;
        }

        .gr-brand__mark {
          display: grid;
          width: 34px;
          height: 34px;
          place-items: center;
          border-radius: 11px;
          color: #fff;
          background: var(--gr-ink);
          font-size: 11px;
          font-weight: 850;
          letter-spacing: -.04em;
        }

        .gr-brand__name {
          font-size: 13px;
          font-weight: 850;
          letter-spacing: .11em;
        }

        .gr-brand__sub {
          margin-top: 1px;
          color: var(--gr-muted);
          font-size: 12px;
          letter-spacing: .04em;
        }

        .gr-sync {
          display: flex;
          align-items: center;
          gap: 7px;
          color: var(--gr-muted);
          font-size: 13px;
          white-space: nowrap;
        }

        .gr-sync::before {
          width: 7px;
          height: 7px;
          border-radius: 999px;
          background: var(--gr-success);
          content: "";
          box-shadow: 0 0 0 4px rgba(22,116,71,.10);
        }

        .gr-sync a {
          color: inherit !important;
          text-decoration: none !important;
        }

        [data-testid="stSelectbox"] {
          margin-bottom: 14px;
        }

        [data-testid="stSelectbox"] [data-baseweb="select"] > div {
          min-height: 52px;
          padding: 0 5px;
          border: 1px solid var(--gr-line);
          border-radius: 15px;
          background: rgba(255,254,250,.84);
          box-shadow: none;
          font-size: 15px;
          font-weight: 750;
        }

        .gr-hero {
          position: relative;
          overflow: hidden;
          padding: 25px 24px 23px;
          border-radius: 24px;
          color: #fff;
          background: #191c17;
          box-shadow: 0 16px 42px rgba(23,25,20,.15);
        }

        .gr-hero::after {
          position: absolute;
          right: -52px;
          bottom: -72px;
          width: 190px;
          height: 190px;
          border: 1px solid rgba(255,255,255,.12);
          border-radius: 999px;
          content: "";
          box-shadow: 0 0 0 25px rgba(255,255,255,.035), 0 0 0 54px rgba(255,255,255,.025);
        }

        .gr-hero--complete {
          background: #123f2a;
        }

        .gr-hero--running {
          background: #17345f;
        }

        .gr-hero--failed {
          background: #52231f;
        }

        .gr-hero__month {
          position: relative;
          z-index: 1;
          color: rgba(255,255,255,.70);
          font-size: 12px;
          font-weight: 700;
          letter-spacing: .08em;
        }

        .gr-hero__score {
          position: relative;
          z-index: 1;
          display: flex;
          align-items: baseline;
          gap: 7px;
          margin: 11px 0 7px;
        }

        .gr-hero__score strong {
          font-size: clamp(48px, 12vw, 64px);
          font-weight: 820;
          letter-spacing: -.07em;
          line-height: .95;
        }

        .gr-hero__score span {
          color: rgba(255,255,255,.72);
          font-size: 14px;
          font-weight: 650;
        }

        .gr-hero__detail {
          position: relative;
          z-index: 1;
          color: rgba(255,255,255,.78);
          font-size: 12px;
          line-height: 1.65;
        }

        .gr-progress {
          margin: 17px 2px 21px;
        }

        .gr-progress__meta {
          display: flex;
          justify-content: space-between;
          margin-bottom: 8px;
          color: var(--gr-muted);
          font-size: 11px;
          font-weight: 700;
        }

        .gr-progress__track {
          overflow: hidden;
          height: 6px;
          border-radius: 999px;
          background: #dedcd5;
        }

        .gr-progress__bar {
          height: 100%;
          border-radius: inherit;
          background: var(--gr-ink);
          transition: width .25s ease;
        }

        .gr-progress--complete .gr-progress__bar { background: var(--gr-success); }
        .gr-progress--running .gr-progress__bar { background: var(--gr-progress); }

        .gr-card {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 14px;
          align-items: center;
          margin: 0 0 10px;
          padding: 17px 17px 16px;
          border: 1px solid var(--gr-line);
          border-radius: var(--gr-radius);
          background: var(--gr-card);
          box-shadow: 0 2px 0 rgba(23,25,20,.02);
        }

        .gr-card--saved { border-color: #c8dfd0; }
        .gr-card--failed { border-color: #efc4be; }
        .gr-card--running { border-color: #c8d5f5; }

        .gr-card__eyebrow {
          overflow: hidden;
          margin-bottom: 4px;
          color: var(--gr-muted);
          font-size: 12px;
          font-weight: 650;
          line-height: 1.4;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .gr-card__title {
          font-size: 17px;
          font-weight: 820;
          letter-spacing: -.02em;
        }

        .gr-card__detail {
          margin-top: 5px;
          color: var(--gr-muted);
          font-size: 13px;
          line-height: 1.55;
        }

        .gr-card__file {
          overflow: hidden;
          max-width: 360px;
          margin-top: 4px;
          color: var(--gr-success);
          font-size: 12px;
          font-weight: 650;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .gr-card__side {
          display: flex;
          flex-direction: column;
          align-items: flex-end;
          gap: 8px;
        }

        .gr-chip {
          padding: 6px 9px;
          border-radius: 999px;
          color: #63665f;
          background: #eeece6;
          font-size: 12px;
          font-weight: 800;
          white-space: nowrap;
        }

        .gr-chip--check { color: var(--gr-success); background: var(--gr-success-soft); }
        .gr-chip--failed { color: var(--gr-danger); background: var(--gr-danger-soft); }
        .gr-chip--running { color: var(--gr-progress); background: var(--gr-progress-soft); }

        .gr-card__link {
          display: inline-flex;
          align-items: center;
          min-height: 44px;
          margin: -8px -8px -8px 0;
          padding: 8px;
          color: var(--gr-ink) !important;
          font-size: 12px;
          font-weight: 800;
          text-decoration: none !important;
        }

        .gr-card__link:hover { text-decoration: underline !important; }

        .gr-card__link:focus-visible,
        .gr-sync a:focus-visible,
        [data-testid="stButton"] > button:focus-visible,
        [data-testid="stLinkButton"] > a:focus-visible {
          outline: 3px solid rgba(31, 83, 163, .35) !important;
          outline-offset: 3px !important;
        }

        .gr-fatal {
          margin: 14px 0;
          padding: 16px 17px;
          border: 1px solid #efc4be;
          border-radius: 17px;
          background: var(--gr-danger-soft);
        }

        .gr-fatal__code {
          color: var(--gr-danger);
          font-size: 11px;
          font-weight: 850;
          letter-spacing: .09em;
        }

        .gr-fatal__title {
          margin: 5px 0 4px;
          color: #692821;
          font-size: 15px;
          font-weight: 820;
        }

        .gr-fatal__detail {
          color: #815049;
          font-size: 13px;
          line-height: 1.6;
        }

        [data-testid="stButton"] > button,
        [data-testid="stLinkButton"] > a {
          min-height: 52px;
          border-radius: 16px !important;
          font-size: 15px !important;
          font-weight: 800 !important;
          box-shadow: none !important;
        }

        [data-testid="stButton"] > button[kind="primary"] {
          border-color: var(--gr-ink) !important;
          color: #fff !important;
          background: var(--gr-ink) !important;
        }

        [data-testid="stButton"] > button[kind="primary"]:hover {
          border-color: #2a2f27 !important;
          background: #2a2f27 !important;
          transform: translateY(-1px);
        }

        [data-testid="stLinkButton"] > a {
          margin-top: 4px;
          border-color: var(--gr-line) !important;
          color: var(--gr-muted) !important;
          background: transparent !important;
        }

        [data-testid="stStatusWidget"] {
          margin: 14px 0;
          border: 1px solid var(--gr-line);
          border-radius: 17px;
          background: var(--gr-card);
        }

        [data-testid="stAlert"] {
          border-radius: 16px;
        }

        @media (max-width: 640px) {
          .block-container { padding: 18px 15px 42px !important; }
          .gr-header { margin-bottom: 16px; }
          .gr-brand__sub { display: none; }
          .gr-hero { padding: 22px 20px 21px; border-radius: 21px; }
          .gr-card { padding: 15px; border-radius: 17px; }
          .gr-card__file { max-width: 230px; }
          .gr-card__eyebrow { max-width: 230px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_compact_header(*, sync_label: str = "Drive未確認", drive_url: str = "") -> None:
    sync = escape(sync_label)
    if drive_url:
        sync = (
            f'<a href="{escape(drive_url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="{sync}（新しいタブでDriveを開く）">{sync}</a>'
        )
    st.markdown(
        f"""
        <div class="gr-header">
          <div class="gr-brand">
            <div class="gr-brand__mark">GR</div>
            <div>
              <div class="gr-brand__name">GETRECEIPT</div>
              <div class="gr-brand__sub">AUTOMATIC RECEIPT COLLECTION</div>
            </div>
          </div>
          <div class="gr-sync">{sync}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_month_hero(
    *,
    month_label: str,
    saved_count: int,
    total_count: int = 4,
    detail: str = "",
    state: str = "ready",
) -> None:
    state_class = state if state in {"complete", "failed", "running"} else "ready"
    st.markdown(
        f"""
        <section class="gr-hero gr-hero--{state_class}">
          <div class="gr-hero__month">{escape(month_label)}</div>
          <div class="gr-hero__score"><strong>{int(saved_count)}</strong><span>/ {int(total_count)} Drive確認済み</span></div>
          <div class="gr-hero__detail">{escape(detail)}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_progress(
    *,
    completed: int,
    total: int = 4,
    active_label: str = "",
    state: str = "idle",
) -> None:
    total = max(int(total), 1)
    completed = max(0, min(int(completed), total))
    percent = round(completed / total * 100)
    right = f"{escape(active_label)}を処理中" if active_label else f"{percent}%"
    st.markdown(
        f"""
        <div class="gr-progress gr-progress--{escape(state)}" role="progressbar"
             aria-label="月次領収書の取得進捗" aria-valuemin="0"
             aria-valuemax="{total}" aria-valuenow="{completed}">
          <div class="gr-progress__meta"><span>MONTHLY RECEIPTS</span><span>{right}</span></div>
          <div class="gr-progress__track"><div class="gr-progress__bar" style="width:{percent}%"></div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_service_card(
    *,
    label: str,
    status: str,
    detail: str = "",
    file_name: str = "",
    drive_url: str = "",
    eyebrow: str = "",
) -> None:
    status_label, status_style = _STATUS.get(status, _STATUS["missing"])
    file_line = f'<div class="gr-card__file">{escape(file_name)}</div>' if file_name else ""
    link = (
        f'<a class="gr-card__link" href="{escape(drive_url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="{escape(label)}のPDFをDriveで開く（新しいタブ）">Driveで開く ↗</a>'
        if drive_url
        else ""
    )
    st.markdown(
        f"""
        <article class="gr-card gr-card--{escape(status)}">
          <div class="gr-card__main">
            <div class="gr-card__eyebrow">{escape(eyebrow)}</div>
            <div class="gr-card__title">{escape(label)}</div>
            <div class="gr-card__detail">{escape(detail)}</div>
            {file_line}
          </div>
          <div class="gr-card__side">
            <span class="gr-chip gr-chip--{status_style}">{escape(status_label)}</span>
            {link}
          </div>
        </article>
        """,
        unsafe_allow_html=True,
    )


def render_fatal_notice(*, title: str, detail: str, code: str = "") -> None:
    st.markdown(
        f"""
        <aside class="gr-fatal" role="alert" aria-live="assertive">
          <div class="gr-fatal__code">{escape(code or 'ERROR')}</div>
          <div class="gr-fatal__title">{escape(title)}</div>
          <div class="gr-fatal__detail">{escape(detail)}</div>
        </aside>
        """,
        unsafe_allow_html=True,
    )
