"""Shared server-rendered presentation primitives for the Web UI."""

from __future__ import annotations

import html
from typing import Literal

BadgeTone = Literal["accent", "danger", "muted", "success", "warning"]

WEB_UI_STYLES = """
:root {
  color-scheme: dark;
  --bg: #0b0d12;
  --bg-soft: #10131a;
  --surface: #151925;
  --surface-raised: #1b2030;
  --surface-hover: #22283a;
  --border: rgba(255, 255, 255, 0.09);
  --border-strong: rgba(255, 255, 255, 0.16);
  --text: #f5f7fb;
  --text-muted: #9ba4b5;
  --accent: #8b7cff;
  --accent-strong: #a79cff;
  --accent-soft: rgba(139, 124, 255, 0.16);
  --success: #67d4e8;
  --success-soft: rgba(103, 212, 232, 0.14);
  --warning: #f3b65d;
  --warning-soft: rgba(243, 182, 93, 0.14);
  --danger: #ff758f;
  --danger-soft: rgba(255, 117, 143, 0.14);
  --shadow: 0 22px 60px rgba(0, 0, 0, 0.28);
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

html {
  background: var(--bg);
}

body {
  min-height: 100vh;
  margin: 0;
  color: var(--text);
  background:
    radial-gradient(circle at 12% 0%, rgba(139, 124, 255, 0.13), transparent 34rem),
    radial-gradient(circle at 90% 12%, rgba(103, 212, 232, 0.08), transparent 28rem),
    var(--bg);
  line-height: 1.5;
}

a {
  color: inherit;
}

a:focus-visible,
button:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 3px solid var(--accent-strong);
  outline-offset: 3px;
}

.shell {
  width: min(1180px, calc(100% - 2rem));
  margin: 0 auto;
  padding: 1.1rem 0 4rem;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  min-height: 4rem;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.75rem;
  color: var(--text);
  font-weight: 760;
  letter-spacing: -0.02em;
  text-decoration: none;
}

.brand-mark {
  display: grid;
  width: 2rem;
  height: 2rem;
  place-items: center;
  border: 1px solid var(--border-strong);
  border-radius: 0.72rem;
  background: linear-gradient(145deg, var(--surface-raised), var(--accent-soft));
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.08);
  color: var(--accent-strong);
  font-size: 0.72rem;
  font-weight: 850;
  letter-spacing: 0.04em;
}

.hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2rem;
  align-items: end;
  padding: clamp(2.7rem, 7vw, 5.8rem) 0 clamp(2rem, 5vw, 3.3rem);
}

.eyebrow {
  margin: 0 0 0.8rem;
  color: var(--accent-strong);
  font-size: 0.77rem;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.hero h1 {
  max-width: 14ch;
  margin: 0;
  font-size: clamp(2.8rem, 7vw, 5.7rem);
  line-height: 0.94;
  letter-spacing: -0.065em;
}

.hero-copy {
  max-width: 43rem;
  margin: 1.25rem 0 0;
  color: var(--text-muted);
  font-size: clamp(1rem, 2vw, 1.15rem);
}

.hero-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.75rem;
}

.button {
  display: inline-flex;
  min-height: 2.8rem;
  align-items: center;
  justify-content: center;
  padding: 0.72rem 1.05rem;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--surface-raised);
  color: var(--text);
  font: inherit;
  font-weight: 750;
  text-decoration: none;
  transition:
    background 120ms ease,
    border-color 120ms ease,
    transform 120ms ease;
}

.button:hover {
  border-color: rgba(255, 255, 255, 0.28);
  background: var(--surface-hover);
  transform: translateY(-1px);
}

.button-primary {
  border-color: transparent;
  background: var(--accent);
  color: #0d0e13;
}

.button-primary:hover {
  border-color: transparent;
  background: var(--accent-strong);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.8rem;
}

.card {
  border: 1px solid var(--border);
  border-radius: 1.15rem;
  background: linear-gradient(165deg, var(--surface-raised), var(--surface));
  box-shadow: var(--shadow);
}

.summary-card {
  min-height: 9rem;
  padding: 1.05rem;
}

.card-label {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.78rem;
  font-weight: 720;
  letter-spacing: 0.045em;
  text-transform: uppercase;
}

.card-value {
  margin: 0.7rem 0 0;
  font-size: 1.18rem;
  font-weight: 780;
  letter-spacing: -0.025em;
}

.card-meta {
  margin: 0.55rem 0 0;
  color: var(--text-muted);
  font-size: 0.84rem;
}

.section {
  margin-top: 2.7rem;
}

.section-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.9rem;
}

.section h2 {
  margin: 0;
  font-size: clamp(1.45rem, 3vw, 2rem);
  letter-spacing: -0.035em;
}

.section-note {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.88rem;
}

.table-card {
  overflow: hidden;
}

.table-scroll {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 0.92rem 1rem;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: middle;
  white-space: nowrap;
}

th {
  color: var(--text-muted);
  background: rgba(255, 255, 255, 0.018);
  font-size: 0.73rem;
  font-weight: 760;
  letter-spacing: 0.055em;
  text-transform: uppercase;
}

tbody tr:last-child td {
  border-bottom: 0;
}

tbody tr:hover td {
  background: rgba(255, 255, 255, 0.018);
}

code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 0.9em;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 0.38rem;
  min-height: 1.7rem;
  padding: 0.2rem 0.58rem;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 0.76rem;
  font-weight: 780;
  line-height: 1;
}

.badge::before {
  width: 0.42rem;
  height: 0.42rem;
  border-radius: 50%;
  background: currentColor;
  content: "";
}

.badge-accent {
  border-color: rgba(167, 156, 255, 0.25);
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.badge-success {
  border-color: rgba(103, 212, 232, 0.24);
  background: var(--success-soft);
  color: var(--success);
}

.badge-warning {
  border-color: rgba(243, 182, 93, 0.24);
  background: var(--warning-soft);
  color: var(--warning);
}

.badge-danger {
  border-color: rgba(255, 117, 143, 0.24);
  background: var(--danger-soft);
  color: var(--danger);
}

.badge-muted {
  border-color: var(--border);
  background: rgba(255, 255, 255, 0.04);
  color: var(--text-muted);
}

.footer {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-top: 2.7rem;
  padding-top: 1.1rem;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8rem;
}

.footer p {
  margin: 0;
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .hero {
    grid-template-columns: 1fr;
  }

  .hero-actions {
    align-items: flex-start;
  }
}

@media (max-width: 620px) {
  .shell {
    width: min(100% - 1.15rem, 1180px);
  }

  .topbar {
    min-height: 3.5rem;
  }

  .hero {
    padding-top: 2.5rem;
  }

  .summary-grid {
    grid-template-columns: 1fr;
  }

  .section-head,
  .footer {
    align-items: flex-start;
    flex-direction: column;
  }

  th,
  td {
    padding: 0.78rem 0.8rem;
  }
}

@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #f5f6fa;
    --bg-soft: #eceef5;
    --surface: #ffffff;
    --surface-raised: #ffffff;
    --surface-hover: #f1f2f7;
    --border: rgba(20, 24, 34, 0.09);
    --border-strong: rgba(20, 24, 34, 0.16);
    --text: #171923;
    --text-muted: #667085;
    --accent: #7564f3;
    --accent-strong: #6553ea;
    --accent-soft: rgba(117, 100, 243, 0.11);
    --success: #167c91;
    --success-soft: rgba(22, 124, 145, 0.1);
    --warning: #99620f;
    --warning-soft: rgba(153, 98, 15, 0.1);
    --danger: #c83d5d;
    --danger-soft: rgba(200, 61, 93, 0.1);
    --shadow: 0 18px 45px rgba(38, 43, 60, 0.08);
  }

  body {
    background:
      radial-gradient(circle at 12% 0%, rgba(117, 100, 243, 0.12), transparent 34rem),
      radial-gradient(circle at 90% 12%, rgba(22, 124, 145, 0.06), transparent 28rem),
      var(--bg);
  }

  .button-primary {
    color: #ffffff;
  }
}
"""


def status_badge(label: str, *, tone: BadgeTone) -> str:
    """Render an escaped compact status badge."""

    return f'<span class="badge badge-{tone}" role="status">{html.escape(label)}</span>'
