from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TZ = ZoneInfo("Europe/Zurich")
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

DEFAULT_EVENT_URLS = [
    "https://cenacolovinciano.vivaticket.it/en/event/cenacolo-vinciano/151991",
    "https://cenacolovinciano.vivaticket.it/en/event/cenacolo-visite-guidate-a-orario-fisso-in-inglese/238363",
]

EN_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
IT_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]

POSITIVE_TOKENS = (
    "available", "availability", "bookable", "posti disponibili",
    "disponibile", "disponibili", "on sale", "in vendita",
)
NEGATIVE_TOKENS = (
    "sold out", "unavailable", "not available", "no availability", "fully booked",
    "not bookable", "disabled", "closed", "esaurito", "esauriti",
    "non disponibile", "non disponibili", "nessuna disponibilità", "completo",
)
TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3])[:.]\d{2}(?!\d)")


@dataclass(frozen=True)
class Config:
    event_urls: list[str]
    target_dates: list[date]
    min_tickets: int
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: list[str]
    test_email: bool
    alert_on_error: bool
    headless: bool


@dataclass(frozen=True)
class Finding:
    event_url: str
    target_date: str
    confidence: str
    evidence: str


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_text(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def parse_target_dates() -> list[date]:
    explicit = os.getenv("TARGET_DATES", "").strip()
    if explicit:
        parsed = sorted({date.fromisoformat(item.strip()) for item in explicit.split(",") if item.strip()})
        if not parsed:
            raise ValueError("TARGET_DATES was supplied but no valid YYYY-MM-DD date was found")
        return parsed

    lookahead = int(os.getenv("LOOKAHEAD_DAYS", "120"))
    weekend_only = env_bool("WEEKEND_ONLY", True)
    start = datetime.now(TZ).date()
    dates: list[date] = []
    for offset in range(1, lookahead + 1):
        candidate = start + timedelta(days=offset)
        if not weekend_only or candidate.weekday() in (5, 6):
            dates.append(candidate)
    return dates


def load_config() -> Config:
    event_urls = [u.strip() for u in os.getenv("EVENT_URLS", ",".join(DEFAULT_EVENT_URLS)).split(",") if u.strip()]
    email_to = [e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()]
    return Config(
        event_urls=event_urls,
        target_dates=parse_target_dates(),
        min_tickets=max(1, int(os.getenv("MIN_TICKETS", "2"))),
        smtp_host=env_text("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(env_text("SMTP_PORT", "465")),
        smtp_username=env_text("SMTP_USERNAME"),
        smtp_password=env_text("SMTP_PASSWORD"),
        email_from=env_text("EMAIL_FROM", env_text("SMTP_USERNAME")),
        email_to=email_to,
        test_email=env_bool("TEST_EMAIL", False),
        alert_on_error=env_bool("ALERT_ON_ERROR", False),
        headless=env_bool("HEADLESS", True),
    )


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).lower()).strip()


def date_variants(d: date) -> list[str]:
    month_en = EN_MONTHS[d.month - 1]
    month_it = IT_MONTHS[d.month - 1]
    return sorted({
        d.isoformat(),
        d.strftime("%d/%m/%Y"),
        f"{d.day}/{d.month}/{d.year}",
        d.strftime("%d-%m-%Y"),
        f"{d.day}-{d.month}-{d.year}",
        f"{month_en} {d.day}, {d.year}",
        f"{d.day} {month_en} {d.year}",
        f"{month_it} {d.day}, {d.year}",
        f"{d.day} {month_it} {d.year}",
    }, key=len, reverse=True)


def text_has_date(text: str, d: date) -> bool:
    value = normalize(text)
    return any(normalize(variant) in value for variant in date_variants(d))


def classify_context(context: str, min_tickets: int) -> tuple[bool, str]:
    value = normalize(context)
    negative_patterns = (
        r'["\']?(?:available|bookable|selectable|onsale)["\']?\s*[:=]\s*false',
        r'["\']?(?:availability|available_seats|seats|tickets|quantity|qty|posti)["\']?\s*[:=]\s*0(?:\D|$)',
        r'["\']?status["\']?\s*[:=]\s*["\']?(?:soldout|sold out|unavailable|closed|full)',
    )
    if any(token in value for token in NEGATIVE_TOKENS) or any(
        re.search(pattern, value) for pattern in negative_patterns
    ):
        return False, "negative availability wording"

    quantity_patterns = (
        r'(?:available|availability|seats?|tickets?|posti|quantity|qty)[^\d]{0,10}(\d{1,3})',
        r'(\d{1,3})[^\d]{0,10}(?:available|seats?|tickets?|posti)',
    )
    for pattern in quantity_patterns:
        for match in re.finditer(pattern, value):
            if min_tickets <= int(match.group(1)) <= 500:
                return True, f"availability quantity appears to be at least {min_tickets}"

    positive_patterns = (
        r'["\']?(?:available|bookable|selectable|onsale)["\']?\s*[:=]\s*true',
        r'["\']?status["\']?\s*[:=]\s*["\']?(?:available|bookable|open|onsale)',
    )
    if any(re.search(pattern, value) for pattern in positive_patterns):
        return True, "structured availability flag is positive"

    if any(token in value for token in POSITIVE_TOKENS):
        return True, "positive availability wording"

    if TIME_RE.search(value):
        return True, "an enabled-looking time slot appears near the date"

    return False, "date found without positive availability evidence"


def surrounding_context(text: str, d: date, radius: int = 450) -> Iterable[str]:
    value = normalize(text)
    for variant in date_variants(d):
        needle = normalize(variant)
        start = 0
        while True:
            index = value.find(needle, start)
            if index < 0:
                break
            yield value[max(0, index - radius): min(len(value), index + len(needle) + radius)]
            start = index + len(needle)


def recursively_find_date_objects(value: Any, d: date, depth: int = 0) -> Iterable[Any]:
    if depth > 8:
        return
    if isinstance(value, dict):
        direct_scalars = {
            key: child
            for key, child in value.items()
            if child is None or isinstance(child, (str, int, float, bool))
        }
        direct_text = json.dumps(direct_scalars, ensure_ascii=False, default=str)
        if text_has_date(direct_text, d):
            yield value
        for child in value.values():
            yield from recursively_find_date_objects(child, d, depth + 1)
    elif isinstance(value, list):
        for child in value[:500]:
            yield from recursively_find_date_objects(child, d, depth + 1)


def inspect_payload(payload_text: str, d: date, min_tickets: int) -> tuple[bool, str] | None:
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        payload = None

    if payload is not None:
        for obj in recursively_find_date_objects(payload, d):
            serialized = json.dumps(obj, ensure_ascii=False, default=str)
            positive, reason = classify_context(serialized, min_tickets)
            if positive:
                return True, f"JSON/API: {reason}"

    for context in surrounding_context(payload_text, d):
        positive, reason = classify_context(context, min_tickets)
        if positive:
            return True, f"network response: {reason}"
    return None


def safe_filename(url: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-")[-90:]
    return f"event-{index}-{slug}"


def dismiss_cookie_banner(page: Page) -> None:
    labels = re.compile(r"^(accept|accept all|allow all|agree|ok|accetta|accetta tutto|consenti)$", re.I)
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=labels)
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=2_000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def maybe_open_booking_interface(page: Page) -> None:
    cta = re.compile(r"buy|book|purchase|tickets?|biglietti|prenota|acquista", re.I)
    try:
        links = page.get_by_role("link", name=cta)
        for i in range(min(links.count(), 8)):
            link = links.nth(i)
            if not link.is_visible():
                continue
            href = link.get_attribute("href")
            if href and not href.lower().startswith(("javascript:", "mailto:")):
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_000)
                return
    except Exception:
        pass

    try:
        buttons = page.get_by_role("button", name=cta)
        for i in range(min(buttons.count(), 8)):
            button = buttons.nth(i)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=5_000)
                page.wait_for_timeout(2_000)
                return
    except Exception:
        pass


def collect_interactive_snapshots(page: Page) -> list[dict[str, Any]]:
    script = """
    () => Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"], input[type="button"], input[type="submit"]'))
      .slice(0, 1500)
      .map((el) => {
        const attrs = {};
        for (const attr of el.attributes || []) {
          if (attr.name.startsWith('data-') || ['aria-label','aria-disabled','title','href','value','datetime'].includes(attr.name)) {
            attrs[attr.name] = attr.value;
          }
        }
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        const hidden = style.display === 'none' || style.visibility === 'hidden' || rect.width === 0 || rect.height === 0;
        const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true' || /(^|\\s)(disabled|unavailable|soldout)(\\s|$)/i.test(el.className || '');
        const parentText = (el.closest('td, li, article, section, div')?.innerText || '').slice(0, 800);
        return {
          text: (el.innerText || el.textContent || '').trim(),
          parentText,
          attrs,
          disabled,
          hidden
        };
      });
    """
    try:
        result = page.evaluate(script)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def detect_from_interactives(
    snapshots: list[dict[str, Any]],
    event_url: str,
    targets: list[date],
    min_tickets: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for d in targets:
        for item in snapshots:
            combined = " ".join([
                str(item.get("text", "")),
                str(item.get("parentText", "")),
                json.dumps(item.get("attrs", {}), ensure_ascii=False),
            ])
            if not text_has_date(combined, d):
                continue
            if item.get("hidden") or item.get("disabled"):
                continue
            positive, reason = classify_context(combined, min_tickets)
            evidence = "enabled date control"
            if positive:
                evidence += f"; {reason}"
            findings.append(Finding(event_url, d.isoformat(), "high", evidence))
            break
    return findings


def check_event(browser: Browser, event_url: str, targets: list[date], min_tickets: int, index: int) -> tuple[list[Finding], dict[str, Any]]:
    context = browser.new_context(
        locale="en-GB",
        timezone_id="Europe/Zurich",
        viewport={"width": 1440, "height": 1100},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    page.set_default_timeout(8_000)
    captured: list[dict[str, str]] = []

    def on_response(response: Any) -> None:
        if len(captured) >= 80:
            return
        try:
            request = response.request
            if request.resource_type not in {"xhr", "fetch", "document"}:
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if not any(token in content_type for token in ("json", "text", "javascript", "html")):
                return
            body = response.text()
            if body and len(body) <= 2_000_000:
                captured.append({"url": response.url, "body": body})
        except Exception:
            return

    page.on("response", on_response)
    stem = safe_filename(event_url, index)
    diagnostic: dict[str, Any] = {"event_url": event_url, "errors": [], "final_url": ""}
    findings: list[Finding] = []

    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)
        dismiss_cookie_banner(page)
        maybe_open_booking_interface(page)
        page.wait_for_timeout(4_000)
        diagnostic["final_url"] = page.url

        body_text = page.locator("body").inner_text(timeout=10_000)
        body_html = page.content()
        snapshots = collect_interactive_snapshots(page)

        findings.extend(detect_from_interactives(snapshots, event_url, targets, min_tickets))
        found_dates = {f.target_date for f in findings}

        for d in targets:
            if d.isoformat() in found_dates:
                continue
            payload_hit = None
            for payload in captured:
                hit = inspect_payload(payload["body"], d, min_tickets)
                if hit:
                    payload_hit = (hit, payload["url"])
                    break
            if payload_hit:
                (_, reason), source_url = payload_hit
                findings.append(Finding(event_url, d.isoformat(), "medium", f"{reason}; source={source_url[:180]}"))
                continue

            for context_text in surrounding_context(body_text, d):
                positive, reason = classify_context(context_text, min_tickets)
                if positive:
                    findings.append(Finding(event_url, d.isoformat(), "low", f"rendered page: {reason}"))
                    break

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACT_DIR / f"{stem}.png"), full_page=True)
        (ARTIFACT_DIR / f"{stem}.html").write_text(body_html, encoding="utf-8")
        (ARTIFACT_DIR / f"{stem}-interactive.json").write_text(
            json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ARTIFACT_DIR / f"{stem}-network.json").write_text(
            json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        page_text_normalized = normalize(body_text)
        if any(token in page_text_normalized for token in ("captcha", "access denied", "forbidden", "bot detection")):
            diagnostic["errors"].append("The site may have presented a CAPTCHA or bot-protection page.")
    except PlaywrightTimeoutError as exc:
        diagnostic["errors"].append(f"Timeout: {exc}")
    except Exception as exc:
        diagnostic["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            diagnostic["final_url"] = page.url
            page.screenshot(path=str(ARTIFACT_DIR / f"{stem}-final.png"), full_page=True)
        except Exception:
            pass
        context.close()

    unique = {(f.event_url, f.target_date): f for f in findings}
    return sorted(unique.values(), key=lambda f: (f.target_date, f.event_url)), diagnostic


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_alert_signature": "", "last_checked_at": "", "last_findings": []}


def write_state(signature: str, findings: list[Finding]) -> None:
    state = {
        "last_alert_signature": signature,
        "last_checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "last_findings": [asdict(f) for f in findings],
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def findings_signature(findings: list[Finding]) -> str:
    canonical = json.dumps(
        sorted((f.event_url, f.target_date, f.confidence) for f in findings),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() if findings else ""


def validate_email_config(config: Config) -> None:
    missing = []
    for name, value in (
        ("SMTP_USERNAME", config.smtp_username),
        ("SMTP_PASSWORD", config.smtp_password),
        ("EMAIL_FROM", config.email_from),
        ("EMAIL_TO", config.email_to),
    ):
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing email settings: " + ", ".join(missing))


def send_email(config: Config, subject: str, plain_body: str, html_body: str) -> None:
    validate_email_config(config)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=30) as server:
            server.login(config.smtp_username, config.smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config.smtp_username, config.smtp_password)
            server.send_message(message)


def build_availability_email(findings: list[Finding], min_tickets: int) -> tuple[str, str, str]:
    by_date: dict[str, list[Finding]] = {}
    for finding in findings:
        by_date.setdefault(finding.target_date, []).append(finding)

    dates = sorted(by_date)
    subject = f"🎟️ 《最后的晚餐》可能有票：{', '.join(dates[:3])}"
    if len(dates) > 3:
        subject += f" 等 {len(dates)} 天"

    plain_lines = [
        "官方 Vivaticket 页面出现了新的可售迹象，请尽快手动打开并完成实名购票。",
        f"监控目标人数：{min_tickets} 人（页面提示不一定能保证仍有连续 {min_tickets} 张）。",
        "",
    ]
    html_items = []
    for d in dates:
        plain_lines.append(d)
        html_links = []
        for finding in by_date[d]:
            plain_lines.append(f"- {finding.confidence}: {finding.event_url}")
            plain_lines.append(f"  证据：{finding.evidence}")
            html_links.append(
                f'<li><a href="{html.escape(finding.event_url)}">打开官方购票页</a> '
                f'（{html.escape(finding.confidence)}；{html.escape(finding.evidence)}）</li>'
            )
        html_items.append(f"<h3>{html.escape(d)}</h3><ul>{''.join(html_links)}</ul>")

    plain_lines.extend([
        "",
        "这是监控提醒，不是自动下单。票务状态可能在你打开页面前发生变化。",
    ])
    html_body = (
        "<p><strong>官方 Vivaticket 页面出现了新的可售迹象。</strong>请尽快手动完成实名购票。</p>"
        f"<p>监控目标人数：{min_tickets} 人；提醒不保证仍有连续 {min_tickets} 张。</p>"
        + "".join(html_items)
        + "<p>这是监控提醒，不是自动下单；票务状态可能迅速变化。</p>"
    )
    return subject, "\n".join(plain_lines), html_body


def main() -> int:
    config = load_config()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    all_findings: list[Finding] = []
    diagnostics: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=config.headless,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
        except Exception:
            # Local fallback when Google Chrome is not installed.
            browser = playwright.chromium.launch(
                headless=config.headless,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )

        try:
            for index, event_url in enumerate(config.event_urls, start=1):
                findings, diagnostic = check_event(
                    browser, event_url, config.target_dates, config.min_tickets, index
                )
                all_findings.extend(findings)
                diagnostics.append(diagnostic)
                time.sleep(1.5)
        finally:
            browser.close()

    unique = {(f.event_url, f.target_date): f for f in all_findings}
    all_findings = sorted(unique.values(), key=lambda f: (f.target_date, f.event_url))
    report = {
        "checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "target_dates": [d.isoformat() for d in config.target_dates],
        "findings": [asdict(f) for f in all_findings],
        "diagnostics": diagnostics,
    }
    (ARTIFACT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    old_state = read_state()
    new_signature = findings_signature(all_findings)
    old_signature = old_state.get("last_alert_signature", "")

    if config.test_email:
        subject = "✅ 《最后的晚餐》监控测试邮件"
        plain = f"监控运行成功。当前发现 {len(all_findings)} 个可能有票的日期/票种组合。"
        html_body = f"<p>监控运行成功。当前发现 <strong>{len(all_findings)}</strong> 个可能有票的日期/票种组合。</p>"
        send_email(config, subject, plain, html_body)
    elif all_findings and new_signature != old_signature:
        subject, plain, html_body = build_availability_email(all_findings, config.min_tickets)
        send_email(config, subject, plain, html_body)

    write_state(new_signature, all_findings)

    errors = [error for diagnostic in diagnostics for error in diagnostic.get("errors", [])]
    if errors:
        print("Diagnostics:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        if config.alert_on_error:
            try:
                send_email(
                    config,
                    "⚠️ 《最后的晚餐》监控运行异常",
                    "\n".join(errors),
                    "<ul>" + "".join(f"<li>{html.escape(e)}</li>" for e in errors) + "</ul>",
                )
            except Exception as exc:
                print(f"Could not send error email: {exc}", file=sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if len(errors) == len(config.event_urls) and not all_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
