#!/usr/bin/env python3
"""Automate reservation POST workflow for gytennis.or.kr."""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

import json
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

try:
    import chromedriver_autoinstaller
except ImportError:
    chromedriver_autoinstaller = None


LOGIN_URL = "https://www.gytennis.or.kr/Login"
RESERVATION_URL = "https://www.gytennis.or.kr/rsvConfirm"
VERIFY_URL = "https://www.gytennis.or.kr/rsvVf"
PAYMENT_POP_URL = "https://spay.kcp.co.kr/kcpPaypop.do?encType="
PAYMENT_CALLBACK_URL = "https://www.gytennis.or.kr/rsvPy"
ORDER_RESULT_URL = "https://www.gytennis.or.kr/ordrRst"

CURL_LOG_FILE: Optional[Path] = None


def quote_for_shell(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_curl_command(method: str, url: str, headers: Dict[str, str], body: Optional[str]) -> str:
    parts: List[str] = ["curl"]
    upper_method = method.upper()
    if upper_method != "GET":
        parts += ["-X", upper_method]
    for key, value in headers.items():
        if value is None:
            continue
        lower = key.lower()
        if lower in {"host", "content-length"}:
            continue
        parts += ["-H", quote_for_shell(f"{key}: {value}")]
    if body:
        parts += ["--data-binary", quote_for_shell(body)]
    parts.append(quote_for_shell(url))
    return " ".join(parts)


def set_curl_log_file(path: Optional[str]) -> None:
    global CURL_LOG_FILE
    if not path:
        CURL_LOG_FILE = None
        return
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if not resolved.exists():
        resolved.touch()
    CURL_LOG_FILE = resolved


def append_curl_log(command: str) -> None:
    if not CURL_LOG_FILE:
        return
    try:
        with CURL_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(command + "\n")
    except OSError:
        pass


class OrderIdParser(HTMLParser):
    """Extract the hidden ordr_idxx value from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.order_id: Optional[str] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "input":
            return
        normalized = {k.lower(): v for k, v in attrs}
        if normalized.get("name") == "ordr_idxx":
            self.order_id = normalized.get("value")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the gytennis reservation workflow after launching a browser window."
    )
    parser.add_argument("--login-userid", required=True, help="userid parameter for the login request.")
    parser.add_argument("--login-password", required=True, help="passwd parameter for the login request.")
    parser.add_argument("--reserve-cvalue", required=True, help="cvalue parameter for the reservation request.")
    parser.add_argument(
        "--reserve-date",
        required=True,
        help="cdate parameter for the reservation request (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--reserve-slot",
        dest="reserve_slot_parts",
        nargs="+",
        required=True,
        help="isvkrr[] parameter parts for the reservation request. Provide either a single quoted value "
        "like '2025-10-22|5|22|8|4000' or five separate tokens '2025-10-22 5 22 8 4000'.",
    )
    parser.add_argument(
        "--reserve-van-code",
        default="",
        help="van_code parameter for the reservation request (leave empty if not required).",
    )
    parser.add_argument(
        "--cookie",
        default="",
        help="Cookie string to pre-populate in the browser (format: name=value; name2=value2).",
    )
    parser.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36",
        help="User-Agent header sent with each request.",
    )
    parser.add_argument(
        "--browser-url",
        default=LOGIN_URL,
        help="URL to open in the local browser before sending HTTP requests.",
    )
    parser.add_argument(
        "--post-login-url",
        default="https://www.gytennis.or.kr/daily",
        help="URL to load after login so the browser reflects the authenticated session.",
    )
    parser.add_argument(
        "--chromedriver-path",
        default=None,
        help="Path to a packaged chromedriver binary. When omitted, the script checks CHROMEDRIVER_PATH and ./drivers/chromedriver.",
    )
    parser.add_argument(
        "--drivers-root",
        default=None,
        help="Root directory that contains per-version chromedriver binaries (e.g. drivers/141/chromedriver).",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Leave the browser window open after automation completes.",
    )
    parser.add_argument(
        "--reuse-browser-tab",
        action="store_true",
        help="Render intermediate pages in the current browser tab instead of opening new tabs.",
    )
    parser.add_argument(
        "--payment-good-name",
        required=True,
        help="good_name parameter for the payment popup request.",
    )
    parser.add_argument(
        "--payment-buyer-name",
        required=True,
        help="buyer_name parameter for the payment popup request.",
    )
    parser.add_argument(
        "--payment-amount",
        required=True,
        help="good_mny parameter (amount) for the payment popup request.",
    )
    parser.add_argument(
        "--payment-url",
        default=PAYMENT_POP_URL,
        help="Endpoint URL for the payment popup request.",
    )
    parser.add_argument(
        "--skip-order-wait",
        action="store_true",
        default=True,
        help="Do not wait for the final order confirmation page (ordrRst) after launching the payment window. (default)",
    )
    parser.add_argument(
        "--wait-order",
        dest="skip_order_wait",
        action="store_false",
        help="Wait for the final order confirmation page (ordrRst).",
    )
    parser.add_argument(
        "--order-wait-timeout",
        type=int,
        default=240,
        help="Seconds to wait for the final order confirmation page after payment (ignored when skip-order-wait is set).",
    )
    parser.add_argument(
        "--curl-log-file",
        default="curl.log",
        help="Optional path to append curl-style request logs emitted during automation.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Request timeout in seconds.",
    )
    return parser.parse_args(argv)


def extract_order_id(html: str) -> str:
    parser = OrderIdParser()
    parser.feed(html)
    parser.close()
    if not parser.order_id:
        raise ValueError("Failed to locate ordr_idxx hidden input in reservation response.")
    return parser.order_id


def coerce_slot(parts: List[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "|".join(parts)


def parse_cookie_header(cookie_header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for chunk in cookie_header.split(";"):
        token = chunk.strip()
        if not token or "=" not in token:
            continue
        name, value = token.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def detect_chrome_major_version() -> Optional[str]:
    """Try to detect the installed Chrome/Chromium major version."""
    env_binary = os.environ.get("CHROME_BINARY")
    candidate_paths: List[Path] = []

    if env_binary:
        candidate_paths.append(Path(env_binary))

    # Check common macOS application bundles.
    candidate_paths.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    )

    # Fall back to PATH lookups for other platforms.
    for binary_name in (
        "google-chrome",
        "chrome",
        "chromium",
        "chromium-browser",
    ):
        located = shutil.which(binary_name)
        if located:
            candidate_paths.append(Path(located))

    for binary_path in candidate_paths:
        if not binary_path or not binary_path.exists():
            continue
        try:
            completed = subprocess.run(
                [str(binary_path), "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            continue

        output = (completed.stdout or completed.stderr or "").strip()
        match = re.search(r"(\d+)\.\d+\.\d+\.\d+", output)
        if match:
            return match.group(1)
    return None


def resolve_chromedriver_path(user_supplied: Optional[str], drivers_root: Optional[str]) -> Optional[str]:
    candidates: List[Path] = []
    if user_supplied:
        candidates.append(Path(user_supplied))

    env_path = os.environ.get("CHROMEDRIVER_PATH")
    if env_path:
        candidates.append(Path(env_path))

    script_dir = Path(__file__).resolve().parent
    drivers_base = Path(drivers_root) if drivers_root else script_dir / "drivers"

    # Try version-specific subdirectories, e.g., drivers/141/chromedriver
    chrome_major = detect_chrome_major_version()
    possible_names = ["chromedriver", "chromedriver.exe"]
    if chrome_major:
        for name in possible_names:
            candidates.append(drivers_base / chrome_major / name)
            candidates.append(drivers_base / f"chrome-{chrome_major}" / name)
            candidates.append(drivers_base / f"v{chrome_major}" / name)

    for name in possible_names:
        candidates.append(drivers_base / name)

    for candidate in candidates:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    auto_path = auto_install_chromedriver(drivers_base)
    if auto_path and Path(auto_path).exists():
        return str(auto_path)
    return None


def auto_install_chromedriver(drivers_base: Path) -> Optional[str]:
    if chromedriver_autoinstaller is None:
        return None
    target_dir = drivers_base / "auto"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        return chromedriver_autoinstaller.install(path=str(target_dir))
    except Exception:
        return None


def launch_browser(
    chromedriver_path: Optional[str],
    user_agent: str,
    keep_browser_open: bool,
    drivers_root: Optional[str],
) -> webdriver.Chrome:
    chrome_options = Options()
    chrome_options.add_argument("--disable-gpu")
    if user_agent:
        chrome_options.add_argument(f"--user-agent={user_agent}")
    if keep_browser_open:
        chrome_options.add_experimental_option("detach", True)

    resolved_path = resolve_chromedriver_path(chromedriver_path, drivers_root)
    chrome_major = detect_chrome_major_version()

    try:
        if resolved_path:
            service = Service(executable_path=resolved_path)
            return webdriver.Chrome(service=service, options=chrome_options)
        return webdriver.Chrome(options=chrome_options)
    except WebDriverException as exc:
        version_hint = f" (감지된 Chrome 메이저 버전: {chrome_major})" if chrome_major else ""
        msg = (
            "Chrome 드라이버를 실행할 수 없습니다."
            f"{version_hint} '--chromedriver-path', 'CHROMEDRIVER_PATH', 또는 "
            "drivers/<버전>/chromedriver 형태로 번들에 포함된 드라이버 경로를 지정해 주세요."
        )
        raise RuntimeError(msg) from exc


def browser_fetch(
    driver: webdriver.Chrome,
    url: str,
    data: Dict[str, str],
    headers: Dict[str, str],
    timeout: float,
) -> Dict[str, str]:
    payload = {key: str(value) for key, value in data.items()}
    allowed_headers = {key: str(value) for key, value in headers.items() if value is not None}
    body_string = urllib.parse.urlencode(payload, doseq=True)
    append_curl_log(build_curl_command("POST", url, allowed_headers, body_string))
    print(
        "[DEBUG] browser_fetch request:",
        json.dumps(
            {
                "url": url,
                "method": "POST",
                "data": payload,
                "headers": allowed_headers,
                "timeout": timeout,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    script = """
        const [url, formData, headerMap, timeoutMs, bodyString] = arguments;
        const done = arguments[arguments.length - 1];
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        const headers = new Headers();
        Object.entries(headerMap || {}).forEach(([key, value]) => headers.append(key, value));
        fetch(url, {
            method: "POST",
            credentials: "include",
            headers,
            body: bodyString,
            signal: controller.signal,
        })
            .then(async (response) => {
                const text = await response.text();
                clearTimeout(timer);
                done({ status: response.status, text });
            })
            .catch((error) => {
                clearTimeout(timer);
                done({ error: error instanceof Error ? error.message : String(error) });
            });
    """
    timeout_ms = max(int(timeout * 1000), 1000)
    result = driver.execute_async_script(script, url, payload, allowed_headers, timeout_ms, body_string)
    if not isinstance(result, dict):
        raise RuntimeError("Browser fetch returned an unexpected result.")
    if "error" in result and result["error"]:
        raise RuntimeError(f"Browser fetch failed: {result['error']}")
    debug_payload = result.copy()
    if "text" in debug_payload and isinstance(debug_payload["text"], str):
        debug_payload["text"] = debug_payload["text"][:2000]
    print("[DEBUG] browser_fetch response:", json.dumps(debug_payload, ensure_ascii=False), file=sys.stderr)
    return result


def ensure_success(step: str, result: Dict[str, str]) -> None:
    status = result.get("status")
    if not status or int(status) >= 400:
        raise RuntimeError(f"{step} returned HTTP status {status}.")


def render_html_in_window(driver: webdriver.Chrome, html: str, window_name: str, reuse_tab: bool) -> None:
    driver.execute_script(
        """
        (function renderPopup(targetName, html, reuseTab) {
            if (!reuseTab) {
                const features = "width=960,height=720,menubar=no,toolbar=no,location=no";
                const popup = window.open("", targetName, features);
                if (!popup) {
                    return;
                }
                popup.document.open();
                popup.document.write(html);
                popup.document.close();
                return;
            }
            const doc = document;
            if (!doc) {
                return;
            }
            const sanitizedName = targetName.replace(/[^a-zA-Z0-9_-]/g, "_");
            const overlayId = "_gyt_overlay_" + sanitizedName;
            let overlay = doc.getElementById(overlayId);
            if (!overlay) {
                overlay = doc.createElement("div");
                overlay.id = overlayId;
                overlay.style.position = "fixed";
                overlay.style.left = "0";
                overlay.style.top = "0";
                overlay.style.width = "100%";
                overlay.style.height = "100%";
                overlay.style.background = "rgba(15, 23, 42, 0.55)";
                overlay.style.zIndex = "2147483647";
                overlay.style.display = "flex";
                overlay.style.alignItems = "center";
                overlay.style.justifyContent = "center";
                overlay.style.padding = "24px";
                overlay.style.boxSizing = "border-box";
                if (doc.body) {
                    overlay.dataset.gytBodyOverflow = doc.body.style.overflow || "";
                    doc.body.style.overflow = "hidden";
                }
                doc.body.appendChild(overlay);
            }
            overlay.innerHTML = "";
            const container = doc.createElement("div");
            container.style.position = "relative";
            container.style.width = "min(960px, 90vw)";
            container.style.height = "min(720px, 90vh)";
            container.style.background = "#fff";
            container.style.borderRadius = "12px";
            container.style.overflow = "auto";
            container.style.boxShadow = "0 24px 48px rgba(15, 23, 42, 0.45)";
            container.style.padding = "0";
            const closeBtn = doc.createElement("button");
            closeBtn.type = "button";
            closeBtn.textContent = "×";
            closeBtn.style.position = "absolute";
            closeBtn.style.top = "12px";
            closeBtn.style.right = "12px";
            closeBtn.style.width = "32px";
            closeBtn.style.height = "32px";
            closeBtn.style.border = "none";
            closeBtn.style.borderRadius = "50%";
            closeBtn.style.background = "rgba(15, 23, 42, 0.15)";
            closeBtn.style.fontSize = "20px";
            closeBtn.style.cursor = "pointer";
            closeBtn.onclick = function() {
                overlay.remove();
                if (doc.body) {
                    doc.body.style.overflow = overlay.dataset.gytBodyOverflow || "";
                }
            };
            const content = doc.createElement("div");
            content.style.width = "100%";
            content.style.height = "100%";
            content.style.overflow = "auto";
            content.innerHTML = html;
            container.appendChild(closeBtn);
            container.appendChild(content);
            overlay.appendChild(container);
        })(arguments[0], arguments[1], arguments[2]);
        """,
        window_name,
        html,
        reuse_tab,
    )


def submit_form_to_window(
    driver: webdriver.Chrome,
    url: str,
    fields: Dict[str, str],
    window_name: str,
    reuse_tab: bool,
) -> None:
    print(
        "[DEBUG] submit_form_to_window:",
        json.dumps(
            {
                "url": url,
                "method": "POST",
                "target": window_name,
                "reuse_tab": reuse_tab,
                "fields": fields,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    body_string = urllib.parse.urlencode(fields, doseq=True)
    current_url = driver.current_url
    parsed_referer = urllib.parse.urlsplit(current_url)
    origin = "https://www.gytennis.or.kr"
    if parsed_referer.scheme and parsed_referer.netloc:
        origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
    payment_log_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "content-type": "application/x-www-form-urlencoded",
        "origin": origin,
        "referer": current_url,
    }
    append_curl_log(build_curl_command("POST", url, payment_log_headers, body_string))
    driver.execute_script(
        """
        (function submitForm(targetUrl, formFields, targetName, reuseTab) {
            if (!reuseTab) {
                const features = "width=960,height=720,menubar=no,toolbar=no,location=no";
                const popup = window.open("about:blank", targetName, features);
                if (!popup) {
                    return;
                }
                const placeholder = popup.document;
                placeholder.open();
                placeholder.write("<html><body style='margin:0;display:flex;align-items:center;justify-content:center;font-family:sans-serif;'>결제창을 불러오는 중입니다...</body></html>");
                placeholder.close();
                const form = document.createElement("form");
                form.method = "POST";
                form.action = targetUrl;
                form.target = targetName;
                form.acceptCharset = "EUC-KR";
                form.enctype = "application/x-www-form-urlencoded";
                Object.entries(formFields || {}).forEach(([name, value]) => {
                    const input = document.createElement("input");
                    input.type = "hidden";
                    input.name = name;
                    input.value = value == null ? "" : String(value);
                    form.appendChild(input);
                });
                document.body.appendChild(form);
                form.submit();
                form.remove();
                return;
            }
            const doc = document;
            if (!doc) {
                return;
            }
            const sanitizedName = targetName.replace(/[^a-zA-Z0-9_-]/g, "_");
            const overlayId = "_gyt_overlay_" + sanitizedName;
            let overlay = doc.getElementById(overlayId);
            if (!overlay) {
                overlay = doc.createElement("div");
                overlay.id = overlayId;
                overlay.style.position = "fixed";
                overlay.style.left = "0";
                overlay.style.top = "0";
                overlay.style.width = "100%";
                overlay.style.height = "100%";
                overlay.style.background = "rgba(15, 23, 42, 0.55)";
                overlay.style.zIndex = "2147483647";
                overlay.style.display = "flex";
                overlay.style.alignItems = "center";
                overlay.style.justifyContent = "center";
                overlay.style.padding = "24px";
                overlay.style.boxSizing = "border-box";
                if (doc.body) {
                    overlay.dataset.gytBodyOverflow = doc.body.style.overflow || "";
                    doc.body.style.overflow = "hidden";
                }
                doc.body.appendChild(overlay);
            }
            overlay.innerHTML = "";
            const container = doc.createElement("div");
            container.style.position = "relative";
            container.style.width = "min(960px, 90vw)";
            container.style.height = "min(720px, 90vh)";
            container.style.background = "#fff";
            container.style.borderRadius = "12px";
            container.style.overflow = "hidden";
            container.style.boxShadow = "0 24px 48px rgba(15, 23, 42, 0.45)";
            container.style.padding = "0";
            const closeBtn = doc.createElement("button");
            closeBtn.type = "button";
            closeBtn.textContent = "×";
            closeBtn.style.position = "absolute";
            closeBtn.style.top = "12px";
            closeBtn.style.right = "12px";
            closeBtn.style.width = "32px";
            closeBtn.style.height = "32px";
            closeBtn.style.border = "none";
            closeBtn.style.borderRadius = "50%";
            closeBtn.style.background = "rgba(15, 23, 42, 0.15)";
            closeBtn.style.fontSize = "20px";
            closeBtn.style.cursor = "pointer";
            closeBtn.onclick = function() {
                overlay.remove();
                if (doc.body) {
                    doc.body.style.overflow = overlay.dataset.gytBodyOverflow || "";
                }
            };
            const frame = doc.createElement("iframe");
            frame.name = targetName;
            frame.style.width = "100%";
            frame.style.height = "100%";
            frame.style.border = "0";
            frame.setAttribute("allow", "payment *");
            frame.setAttribute("title", "결제창");
            container.appendChild(closeBtn);
            container.appendChild(frame);
            overlay.appendChild(container);

            const form = doc.createElement("form");
            form.method = "POST";
            form.action = targetUrl;
            form.target = targetName;
            form.acceptCharset = "EUC-KR";
            form.enctype = "application/x-www-form-urlencoded";
            Object.entries(formFields || {}).forEach(([name, value]) => {
                const input = doc.createElement("input");
                input.type = "hidden";
                input.name = name;
                input.value = value == null ? "" : String(value);
                form.appendChild(input);
            });
            doc.body.appendChild(form);
            form.submit();
            form.remove();
        })(arguments[0], arguments[1], arguments[2], arguments[3]);
        """,
        url,
        fields,
        window_name,
        reuse_tab,
    )


def await_order_result(
    driver: webdriver.Chrome,
    timeout_seconds: int,
    main_handle: Optional[str],
    payment_handle: Optional[str],
) -> Optional[str]:
    deadline = time.time() + timeout_seconds
    detected_url: Optional[str] = None

    while time.time() < deadline and detected_url is None:
        for handle in list(driver.window_handles):
            try:
                driver.switch_to.window(handle)
                current_url = driver.current_url
            except WebDriverException:
                continue
            if "ordrRst" in current_url:
                detected_url = current_url
                break
        time.sleep(1)

    try:
        target = None
        if payment_handle and payment_handle in driver.window_handles:
            target = payment_handle
        elif main_handle and main_handle in driver.window_handles:
            target = main_handle
        if target:
            driver.switch_to.window(target)
    except WebDriverException:
        pass

    print(
        "[DEBUG] await_order_result outcome:",
        json.dumps({"detected_url": detected_url}, ensure_ascii=False),
        file=sys.stderr,
    )
    return detected_url


def wait_for_payment_window(
    driver: webdriver.Chrome,
    existing_handles: List[str],
    target_name: str,
    timeout_seconds: float,
) -> str:
    known_handles = set(existing_handles)
    deadline = time.time() + timeout_seconds
    target_handle: Optional[str] = None

    while time.time() < deadline:
        current_handles = driver.window_handles
        for handle in current_handles:
            if handle in known_handles:
                continue
            try:
                driver.switch_to.window(handle)
            except WebDriverException:
                continue
            window_name = ""
            try:
                window_name = driver.execute_script("return window.name || '';")
            except WebDriverException:
                window_name = ""
            current_url = ""
            try:
                current_url = driver.current_url
            except WebDriverException:
                current_url = ""
            if window_name == target_name or "spay.kcp.co.kr" in current_url:
                target_handle = handle
                break
        if target_handle:
            break
        time.sleep(0.5)

    if not target_handle:
        raise TimeoutError("Failed to detect payment popup window.")

    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except (TimeoutException, WebDriverException):
        pass
    return target_handle


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    set_curl_log_file(args.curl_log_file)

    if args.reuse_browser_tab:
        print(
            "[INFO] --reuse-browser-tab 옵션은 비활성화되며 결제 팝업은 새 창으로 열립니다.",
            file=sys.stderr,
        )
        args.reuse_browser_tab = False

    slot_value = coerce_slot(args.reserve_slot_parts)
    driver: Optional[webdriver.Chrome] = None
    order_id: Optional[str] = None
    verify_result: Optional[Dict[str, str]] = None
    payment_result: Optional[Dict[str, str]] = None
    order_result_url: Optional[str] = None
    payment_handle: Optional[str] = None
    main_window_handle: Optional[str] = None

    try:
        driver = launch_browser(
            args.chromedriver_path,
            args.user_agent,
            args.keep_browser_open,
            args.drivers_root,
        )
        driver.get(args.browser_url)
        main_window_handle = driver.current_window_handle

        if args.cookie:
            for name, value in parse_cookie_header(args.cookie).items():
                driver.add_cookie({"name": name, "value": value})
            driver.get(args.browser_url)

        common_form_accept = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        origin_host = "https://www.gytennis.or.kr"

        login_headers = {
            "Accept": common_form_accept,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": origin_host,
            "Referer": args.browser_url,
        }
        login_payload = {"userid": args.login_userid, "passwd": args.login_password}
        login_result = browser_fetch(driver, LOGIN_URL, login_payload, login_headers, args.timeout)
        ensure_success("Login request", login_result)

        # Load a page so the user sees the logged-in state once the automation finishes.
        driver.get(args.post_login_url)

        reservation_headers = {
            "Accept": common_form_accept,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": origin_host,
            "Referer": args.post_login_url,
        }
        reservation_payload = {
            "cvalue": args.reserve_cvalue,
            "cdate": args.reserve_date,
            "isvkrr[]": slot_value,
            "van_code": args.reserve_van_code,
        }
        reservation_response = browser_fetch(
            driver,
            RESERVATION_URL,
            reservation_payload,
            reservation_headers,
            args.timeout,
        )
        ensure_success("Reservation request", reservation_response)
        reservation_html = reservation_response["text"]
        order_id = extract_order_id(reservation_html)
        driver.execute_script("document.open();document.write(arguments[0]);document.close();", reservation_html)
        try:
            WebDriverWait(driver, args.timeout).until(
                lambda d: d.find_element("name", "ordr_idxx")
            )
        except TimeoutException:
            print("[WARN] 결제 준비 페이지 로드 대기 중 요소를 확인하지 못했습니다.", file=sys.stderr)

        driver.execute_script(
            """
            const form = document.forms && document.forms.length ? document.forms[0] : document.querySelector("form");
            if (form) {
                if (form.good_name) form.good_name.value = arguments[0];
                if (form.buyr_name) form.buyr_name.value = arguments[1];
                if (form.good_mny) form.good_mny.value = arguments[2];
            }
            """,
            args.payment_good_name,
            args.payment_buyer_name,
            args.payment_amount,
        )

        existing_handles = list(driver.window_handles)
        trigger_result = driver.execute_script(
            """
            const candidates = [
                typeof fnPay === "function" ? fnPay :
                (typeof fn_pay === "function" ? fn_pay :
                (typeof pay === "function" ? pay : null))
            ];
            for (const fn of candidates) {
                if (typeof fn === "function") {
                    try {
                        fn();
                        return "function";
                    } catch (err) {
                        return "error:" + err;
                    }
                }
            }
            const clickable = Array.from(document.querySelectorAll("a,button,input[type='button'],input[type='submit']"));
            for (const el of clickable) {
                const handler = (el.getAttribute("onclick") || "").toLowerCase();
                if (handler.includes("pay") || handler.includes("payment")) {
                    try {
                        el.click();
                        return "click";
                    } catch (err) {
                        return "error:" + err;
                    }
                }
            }
            return "notfound";
            """
        )
        if isinstance(trigger_result, str) and trigger_result.startswith("error:"):
            raise RuntimeError(f"Failed to trigger payment flow: {trigger_result}")
        if trigger_result == "notfound":
            raise RuntimeError("Could not locate payment trigger on the reservation page.")

        payment_result = {"status": "triggered"}

        try:
            payment_handle = wait_for_payment_window(
                driver,
                existing_handles,
                "KCPPayPopup",
                args.timeout,
            )
            driver.switch_to.window(payment_handle)
        except TimeoutError as exc:
            print(f"Failed to detect payment popup: {exc}", file=sys.stderr)

        if not args.skip_order_wait:
            print(
                f"결제 완료 확인 페이지를 기다리는 중입니다 (최대 {args.order_wait_timeout}초)...",
                file=sys.stderr,
            )
            order_result_url = await_order_result(
                driver,
                args.order_wait_timeout,
                main_window_handle,
                payment_handle,
            )
    except WebDriverException as exc:
        print(f"Browser automation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed to complete workflow: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver and not args.keep_browser_open:
            try:
                driver.quit()
            except Exception:
                pass

    print(f"Reservation verified with id '{order_id}'.")
    print(f"Verification response status: {verify_result.get('status') if verify_result else 'n/a'}")
    print(f"Payment popup status: {payment_result.get('status') if payment_result else 'n/a'}")
    if not args.skip_order_wait:
        if order_result_url:
            print(f"Final order confirmation detected at '{order_result_url}'.")
        else:
            print(
                "Timed out waiting for the order confirmation page. 결제창에서 진행이 끝나지 않았다면 계속 진행해 주세요."
            )
    if args.keep_browser_open:
        print("자동화 완료 후에도 브라우저 창을 열어 두었습니다.")
    else:
        print("자동화가 끝난 뒤 브라우저 세션을 종료했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
