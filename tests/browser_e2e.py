#!/usr/bin/env python3
"""browser_e2e.py — real-Firefox IndexedDB end-to-end test.

Drives the offline demo page (public/index.html) headlessly through geckodriver:

  AC1  create/edit/delete docs, reopen persists (IndexedDB)
  AC2  no plaintext in IndexedDB (inspect raw records)
  AC3  no access without the correct master password
  AC6  pure offline demo page (no server dependency beyond static files)

Run:  .venv/bin/python tests/browser_e2e.py
Prereq: firefox + geckodriver on PATH; start the demo server first
       (node --enable-source-maps server/demo.mjs &)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:4242")

# Resolve the local geckodriver deterministically: Selenium Manager would
# otherwise dial github.com / plausible.io on every driver launch, which fails
# on offline CI (tls handshake eof) and stalls driver creation. With the
# offline marker set, selenium falls back to the PATH-resolved geckodriver
# with no network calls.
os.environ.setdefault("SE_MANAGER_OFFLINE", "1")


def find_firefox_binary():
    """Locate a Firefox executable usable by geckodriver/selenium.

    On Ubuntu, /usr/bin/firefox is a wrapper script for the snap, which
    selenium rejects ("binary is not a Firefox executable"). Allow an
    explicit override and fall back to the snap's inner binary.
    """
    candidates = []
    if os.environ.get("FIREFOX_BIN"):
        candidates.append(os.environ["FIREFOX_BIN"])
    import glob as _glob

    candidates.extend(
        sorted(
            _glob.glob("/snap/firefox/*/usr/lib/firefox/firefox"),
            key=os.path.getmtime,
            reverse=True,
        )
    )
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


KNOWN_TITLE = "Project Snowstorm Operations"
KNOWN_BODY = "The rendezvous coordinates are 48.8584N, 2.2945E; access code oscar-7."
KNOWN_EDIT = "UPDATED: deflector polarity reversed."


def wait_for(driver, fn, timeout=10):
    return WebDriverWait(driver, timeout).until(fn)


def fresh_profile():
    opts = Options()
    opts.headless = True
    opts.add_argument("-profile")
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix="ffkb-")
    opts.add_argument(tmp)
    # In-memory-only profile storage so tests are deterministic
    prefs = {
        "profile.content_settings.exceptions.automatic_downloads": "*",
    }
    return opts, tmp


def get_db_dump(driver):
    """Serialize every IndexedDB record in the page's DB (local-kb)."""
    script = """
    const cb = arguments[arguments.length - 1];
    (async () => {
      const req = indexedDB.open('local-kb');
      const db = await new Promise((res, rej) => {
        req.onsuccess = () => res(req.result);
        req.onerror = () => { res(null); };
      });
      if (!db) return cb({ error: true });
      const names = Array.from(db.objectStoreNames);
      const all = {};
      for (const n of names) {
        all[n] = await new Promise((res) => {
          const tx = db.transaction(n, 'readonly');
          const q = tx.objectStore(n).getAll();
          q.onsuccess = () => res(q.result);
          q.onerror = () => res([]);
        });
      }
      db.close();
      cb(all);
    })().catch((e) => cb({ error: true, msg: String(e) }));
    """
    return driver.execute_async_script(script)


def flatten_dump(d):
    """All string values from every store, concatenated into one blob."""
    parts = []
    for name, recs in d.items():
        if not isinstance(recs, list):
            continue
        for r in recs:
            if isinstance(r, dict):
                for k, v in r.items():
                    parts.append(str(v))
            else:
                parts.append(str(r))
    return "\n".join(parts)


def main():
    print("==> launching Firefox (headless) against", BASE)
    opts = Options()
    opts.headless = True
    fb = find_firefox_binary()
    if fb is None:
        print("! no Firefox binary found; refusing to run")
        sys.exit(2)
    opts.binary_location = fb  # snap wrapper /usr/bin/firefox is not binary
    opts.set_preference("dom.indexedDB.enabled", True)
    driver = webdriver.Firefox(options=opts)
    driver.set_page_load_timeout(20)

    failures = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  ok  {name}")
        else:
            print(f"FAIL  {name}  {detail}")
            failures.append(name)

    try:
        driver.get(BASE + "/")
        # wait for main.js to have run: the unlock view becomes visible only
        # after the module executes and calls setView() (CSS hides all .view)
        wait_for(
            driver,
            EC.visibility_of_element_located((By.ID, "view-unlock")),
        )
        check("demo page loads offline (unlock view present)", True)

        # ---- AC3: no access without password
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check(
            "unlock with empty password is blocked",
            "password" in log.lower() and "master" in log.lower(),
            log,
        )

        # ---- create-time master-password gate (用户手册 3.1 requires setting
        # the master password "并再次确认"; 4.1 可配置项 lists 主密码 "≥ 8 字符").
        # A too-short password must be refused before any key derivation runs.
        sn_short = driver.find_element(By.ID, "wsname")
        sn_short.send_keys("ops-vault")
        pw_short = driver.find_element(By.ID, "pw2")
        pw_short.send_keys("short1")  # 7 chars — below the 8-char floor
        cfm_short = driver.find_element(By.ID, "pw2b")
        cfm_short.send_keys("short1")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused when the master password is shorter than 8 chars",
              "at least 8" in log.lower(), log)
        sn_short.clear()
        pw_short.clear()
        cfm_short.clear()

        # ---- create workspace (confirm field: a typo is unrecoverable)
        wsname = driver.find_element(By.ID, "wsname")
        wsname.send_keys("ops-vault")
        pw = driver.find_element(By.ID, "pw2")
        pw.send_keys("correct horse battery staple")
        mismatch = driver.find_element(By.ID, "pw2b")
        mismatch.send_keys("correct horse battery staple but different")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check(
            "create refused when the two passwords differ",
            "do not match" in log.lower(),
            log,
        )
        mismatch.clear()
        mismatch.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-create").click()
        # view-app exists in static HTML but is display:none until the async
        # PBKDF2 (310k iters) key derivation finishes and setView() runs — wait
        # for actual visibility, not DOM presence.
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        check("workspace created, app view shown", True)
        label = driver.find_element(By.ID, "ws-label").text
        check("workspace name surfaced in app header", "ops-vault" in label, label)

        # ---- create is refused while a vault exists (never orphan it)
        # The workspace name participates in HKDF key derivation, so a nameless
        # create must be refused outright (lock first to return to the gate).
        driver.find_element(By.ID, "btn-lock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        wsname.clear()
        driver.find_element(By.ID, "pw2").send_keys("another dungeon master key")
        driver.find_element(By.ID, "pw2b").send_keys("another dungeon master key")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused with no workspace name", "name" in log.lower(), log)

        # Even a fully-filled create is refused over the live vault (creating
        # again would replace the workspace params and orphan every doc).
        wsname.send_keys("ops-vault")
        driver.find_element(By.ID, "btn-create").click()
        time.sleep(0.8)
        log = driver.find_element(By.ID, "log").text
        check("create refused when a vault already exists",
              "already exists" in log.lower(), log)

        # The refused creates must not have touched the live vault: re-unlock
        # with the real master password and confirm the workspace survives.
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        label = driver.find_element(By.ID, "ws-label").text
        check("refused creates left existing vault intact", "ops-vault" in label, label)

        # ---- create a doc with known plaintext
        # wait until the backend has finished unlocking and the doc list UI
        # is interactive (btn-new is only enabled after setView renders)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(1.0)
        title = driver.find_element(By.ID, "editor-title")
        title.clear()
        title.send_keys(KNOWN_TITLE)
        area = driver.find_element(By.ID, "editor-area")
        area.clear()
        area.send_keys(KNOWN_BODY)
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.5)
        check("document created and saved", "#" not in driver.find_element(By.ID, "log").text)

        # reopen: reload page (simulates closing the browser)
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        check("fresh page shows locked state (AC1 prerequisite)", True)

        # wrong password: still locked
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("definitively-wrong-password")
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("AC3: wrong password denied", "Wrong master password" in log, log)

        # ---- repeated failures stay non-destructive (用户手册 5.1 故障 1: "重复
        # 失败不会损害数据"). Two more consecutive wrong attempts with *different*
        # passwords must each be denied at the verifier and keep the lock screen
        # up; the positive control (the correct password still unlocks the intact
        # vault) is the AC1 check right below.
        for i, wrong in enumerate(["definitively-wrong-password-2",
                                   "definitively-wrong-password-3"], start=2):
            pw.clear()
            pw.send_keys(wrong)
            driver.find_element(By.ID, "btn-unlock").click()
            time.sleep(1.2)
            log = driver.find_element(By.ID, "log").text
            check(f"AC3: repeated wrong password #{i} denied (manual 5.1 故障 1)",
                  "Wrong master password" in log, log)
        check("AC3: repeated failures keep the lock screen up",
              driver.find_element(By.ID, "view-app").get_attribute("style").find("block") == -1,
              driver.find_element(By.ID, "view-app").get_attribute("style"))

        # correct password: opens and shows doc
        pw.clear()
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        # doc rows are rendered after the (async) store reindex completes
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC1: doc list shows persisted title after reopen",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # ---- edit persists
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        docs[0].click()
        time.sleep(1.0)
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(KNOWN_EDIT)
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)

        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        docs[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("edit persisted after reopen", KNOWN_EDIT in snippet)

        # ---- Ctrl/Cmd+S shortcut persists (用户手册 3.3: "点击“保存”（或按
        # Ctrl/Cmd + S）"). Drive the document-level keydown handler with a
        # synthesized ctrl+s (the handler is a document keydown listener and
        # does not inspect isTrusted), then prove the save really persisted by
        # reloading and reopening — an AC1-strength proof for the shortcut.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nCTRL-S-AUTOSAVE-PROOF-77")
        time.sleep(0.3)
        check("typing arms the unsaved-changes guard",
              "edited" in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key:'s', ctrlKey:true, bubbles:true}))"
        )
        time.sleep(1.2)
        check("ctrl+s shows the saved receipt",
              "Saved" in driver.find_element(By.ID, "log").text)
        check("ctrl+s clears the dirty flag",
              "edited" not in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("ctrl+s persisted through reload (AC1 proof for the shortcut)",
              "CTRL-S-AUTOSAVE-PROOF-77" in snippet, snippet[-120:])

        # ---- Cmd+S (macOS Meta) half of the SAME documented shortcut.
        # 用户手册 3.3 says "点击“保存”（或按 Ctrl/Cmd + S）": the handler in
        # public/main.js:531 accepts (e.ctrlKey || e.metaKey) && key==='s', but
        # every check above synthesizes ctrlKey only. Pin the Cmd/metaKey branch
        # end-to-end: an unsaved edit is saved by a synthesized meta+s, the dirty
        # flag clears, and the stored body grows — the AC1-strength persistence
        # proof for the documented macOS modifier.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nCMDS-METAKEY-PROOF-91")
        time.sleep(0.3)
        check("cmd+s (metaKey) arms the unsaved-changes guard before dispatch",
              "edited" in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key:'s', metaKey:true, bubbles:true}))"
        )
        time.sleep(1.2)
        check("cmd+s (metaKey) shows the saved receipt (manual 3.3 Ctrl/Cmd + S)",
              "Saved" in driver.find_element(By.ID, "log").text)
        check("cmd+s (metaKey) clears the dirty flag",
              "edited" not in driver.find_element(By.ID, "doc-meta").text.lower())
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("cmd+s (metaKey) persisted through reload (AC1 proof for the shortcut)",
              "CMDS-METAKEY-PROOF-91" in snippet, snippet[-120:])

        # ---- page-leave reminder mechanism (用户手册 3.3: "未保存的修改在……
        # 离开页面时会收到提醒"). The new-doc/switch-doc prompts are covered
        # above; this pins the browser-level guard: window.onbeforeunload must
        # be armed exactly while the editor is dirty, and disarmed by a save.
        check("clean editor does not arm the page-leave guard",
              driver.execute_script("return window.onbeforeunload === null"))
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nBEFOREUNLOAD-DIRTY")
        time.sleep(0.3)
        check("dirty editor arms the page-leave guard",
              driver.execute_script("return window.onbeforeunload !== null"))
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        check("save disarms the page-leave guard",
              driver.execute_script("return window.onbeforeunload === null"))

        # ---- P1 editor: markdown preview toggle + XSS safety (real browser)
        # The current doc is open in the editor at this point.
        area = driver.find_element(By.ID, "editor-area")
        # 用户手册 3.5: preview 支持 fenced code (``` / ~~~)、无序与有序列表、
        # 引用（含嵌套层级 ">>"），并自动屏蔽 javascript:、data: 等危险链接。
        area.send_keys(
            '\n## MD Preview Check\n**bold** `code` *italic* __emph__ [bad](javascript:alert(1))\n<script>alert(1)</script>'
            '\n```\nconst x = 1;\n```\n'
            '\n- alpha\n- beta\n* star\n+ plus\n'
            '\n1. first\n2. second\n1) one\n2) two\n'
            '\n> quote\n>> nested\n'
            '\n[data](data:image/png;base64,AAAA)'
            '\n[ok](https://example.com/path)'
            '\n[mail](mailto:a@b.com)'
            '\n[anchor](#sec)'
            '\n[rel](/rel/path)'
            '\nplain paragraph text'
            '\n---'
            '\n___'
        )
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("preview renders markdown headings", "<h2>MD Preview Check</h2>" in inner, inner[:200])
        # Manual 3.5 编辑器支持 the inline spans the editor produces. Every
        # span leg the editor can emit is pinned positively below: **bold**
        # and __emph__ -> <strong>, `code` -> <code>, *italic* -> <em>. The
        # <em> assert exercises the italic regex markdown.js:38 directly;
        # the other <em> asserts in this preview block come from the
        # *link-degradation* branch (markdown.js:44) only. A regression in
        # any inline span regex fails a check.
        check("preview renders inline formatting",
              "<strong>bold</strong>" in inner
              and "<strong>emph</strong>" in inner
              and "<code>code</code>" in inner
              and "<em>italic</em>" in inner, inner[:300])
        check("preview escapes raw HTML inert", "<script>alert" not in inner and "&lt;script&gt;" in inner)
        check("preview rejects javascript: links", "<em>bad</em>" in inner and "javascript:" not in inner)
        check("preview renders fenced code block",
              "<pre><code>const x = 1;</code></pre>" in inner, inner[:200])
        # 用户手册 3.5 / 设计说明书 3.8: 无序/有序列表. markdown.js:178-179
        # accept the full marker character classes [-*+] (ul) and \d+[.)] (ol),
        # but the existing input pinned only the `-` and `.` legs — dropping
        # `*`, `+`, or `)` from the regexes would leave every current check
        # green. So `*`/`+` bullets and `)` ordered markers must render into
        # the same single <ul>/<ol> block (contiguous markers after the
        # existing - / 1. items), with no stray <p> wrapper around them.
        check("preview renders unordered list markers - * + (manual 3.5)",
              "<ul><li>alpha</li><li>beta</li><li>star</li><li>plus</li></ul>" in inner
              and "* star" not in inner, inner[:300])
        check("preview renders ordered list markers . ) (manual 3.5)",
              "<ol><li>first</li><li>second</li><li>one</li><li>two</li></ol>" in inner
              and "1) one" not in inner, inner[:300])
        check("preview renders quote with nested depth",
              "<blockquote>quote</blockquote>" in inner
              and "<blockquote>&gt; nested</blockquote>" in inner, inner[:300])
        check("preview masks data: links as inert text",
              "<em>data</em>" in inner and "data:image/png" not in inner, inner[:300])
        # Manual 3.5 预览 promises "自动屏蔽 javascript:、data: 等危险链接"
        # — the negatives above (js/data degrade to <em>) are pinned, but nothing
        # pins the allowlist positive: a SAFE_URL (markdown.js:23) regression
        # that killed *every* link (e.g. SAFE_URL inverted or dropped) would
        # leave all the above checks green. So each allowlist leg — https:,
        # mailto:, fragment #, and bare / relative — must render as a real
        # anchor with label + href, never degrade to <em>.
        check("preview keeps safe https: links as real anchors (manual 3.5)",
              "<a href=\"https://example.com/path\">ok</a>" in inner
              and "<em>ok</em>" not in inner, inner[:300])
        # The SAFE_URL allowlist above also takes mailto:, #-fragment, and
        # bare-/ relative targets — the same class of pin as the https: leg:
        # dropping any of these legs (a "simplified" SAFE_URL like /^https?:/)
        # would leave every existing anchor check green.
        check("preview renders mailto/fragment/relative links as anchors (设计说明书 3.8)",
              "<a href=\"mailto:a@b.com\">mail</a>" in inner
              and "<a href=\"#sec\">anchor</a>" in inner
              and "<a href=\"/rel/path\">rel</a>" in inner, inner[:300])
        # Manual 3.5 编辑器支持 lists 段落 (paragraph) among the supported
        # constructs. The script tag above proves an escaped <p> is emitted
        # (line 434), but nothing pins the element itself: a wrapper regression
        # from <p> to another block element (or losing the wrapper) would pass
        # every current check. So a distinctive bare text line must render as
        # exactly one <p>.</p>.
        check("preview renders plain paragraph as <p> (manual 3.5)",
              "<p>plain paragraph text</p>" in inner, inner[:300])
        # 设计说明书 3.8: renderMarkdown 渲染 "标题、水平线、围栏代码块、块引用、
        # 无序/有序列表、管道表格". Every other named block is pinned above
        # (heading/paragraph/quote/fence/ul/ol/table), but the horizontal rule
        # (markdown.js:144-150) has no browser pin — a regression that let `---`
        # fall through to the paragraph branch would pass every existing check.
        # So a lone `---` line must render as exactly one <hr>, with no <p>
        # wrapper around it. The `___` variant (markdown.js:145 character class
        # `[-*_]`) was pinned nowhere — not in the browser (the new `---` pin)
        # nor even at the Node unit level (markdown.test.mjs:33-36 pins only
        # `---` and `***`). Dropping `_` from the regex is exactly the kind of
        # "simplification" that would leave all current pins green, so `___`
        # must also render as exactly one <hr> with no <p> wrapper.
        check("preview renders both hr variants --- and ___ as <hr> (设计说明书 3.8)",
              inner.count("<hr>") == 2 and "<p>---</p>" not in inner and "<p>___</p>" not in inner,
              f"hrs={inner.count('<hr>')} {inner[:300]}")
        check("preview toggle button flips to Edit", driver.find_element(By.ID, "btn-preview").text == "Edit")
        driver.find_element(By.ID, "btn-preview").click()  # back to editor
        time.sleep(0.4)
        check("preview toggle returns to editor",
              driver.find_element(By.ID, "editor-area").is_displayed())

        # ---- P1 editor: starting a new doc prompts when edits are unsaved
        # (用户手册 3.3: "未保存的修改在切换文档或离开页面时会收到提醒"). The
        # editor above still holds an unsaved edit (the MD-preview text), so
        # clicking New doc must confirm — dismissing keeps the document.
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        check("new doc prompts to discard unsaved edits", True)
        alert.dismiss()
        time.sleep(0.3)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("new-doc prompt: dismissing keeps the unsaved document",
              KNOWN_TITLE in title_val and "MD Preview Check" in area_val,
              (title_val, area_val[-120:]))
        check("new-doc prompt: no throwaway doc was created",
              not any("Untitled" in (r.text or "") for r in
                      driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")))

        # ---- table blocks: pipe rows round-trip editor -> encrypted store
        area = driver.find_element(By.ID, "editor-area")
        # Row 4 uses an escaped pipe inside a cell (用户手册 5.1 故障 7):
        # `\|` must stay in-cell and round-trip the encrypted store.
        area.send_keys("\n\n| city | country |\n| --- | --- |\n| Kyoto | Japan |\n| Osaka \\| OSK | Japan |")
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("table block previews as <table>",
              "<table>" in inner and "<th>city</th>" in inner and "<td>Kyoto</td>" in inner,
              inner[:200])
        check("table block escaped pipe stays one cell",
              "<td>Osaka | OSK</td>" in inner and "\\|" not in inner and "<td>Osaka \\</td>" not in inner,
              inner[:300])
        # 用户手册 5.1 故障 7 also promises "表格不会被拆散成普通段落": the six
        # pipe rows must render as exactly one <table>, with no cell content
        # leaking out as <p> fragments (renderMarkdown would emit <p>| city |…
        # only if a row fell through to the paragraph branch).
        check("table stays one <table> block, not split into paragraphs (manual 5.1 故障 7)",
              inner.count("<table>") == 1
              and "<p>|" not in inner
              and not any(c in re.sub(r"<table>.*?</table>", "", inner, flags=re.S)
                          for c in ["city", "country", "Kyoto", "Japan", "Osaka | OSK"]),
              f"tables={inner.count('<table>')} {inner[:300]}")
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.4)
        # reload the doc: blocks -> markdown must keep the pipe rows after the
        # save/encrypt/decrypt round-trip through the store.
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("table round-trips from encrypted store",
              "| Kyoto | Japan |" in snippet, snippet[-300:])
        check("table escaped pipe round-trips from encrypted store",
              "Osaka \\| OSK" in snippet, snippet[-300:])

        # ---- heading levels #..######, nested quotes >/>>, ~~~ fences all
        # round-trip losslessly — 用户手册 3.5: "标题的级别（## 至 ######）
        # 与嵌套引用（>>）同样无损保留"; 编辑器支持 代码块（``` 或 ~~~)。
        # The table round-trip above proves verbatim persistence, but its
        # marker lines only pin "##" and a backtick fence — so drive every
        # heading level, a nested blockquote pair, and the tilde fence through
        # save -> encrypt -> decrypt -> reload back into the editor, then
        # preview them all from the store-derived plaintext.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(
            "\n# H1 level\n## H2 level\n### H3 level\n#### H4 level\n"
            "##### H5 level\n###### H6 level\n"
            "> outer level\n>> inner level\n"
            "~~~\nprint('tilde fence')\n~~~"
        )
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("heading levels #..###### round-trip from encrypted store",
              all(f"{'#'*n} H{n} level" in snippet for n in range(1, 7)),
              snippet[-400:])
        check("tilde fence round-trips from encrypted store",
              "~~~\nprint('tilde fence')\n~~~" in snippet, snippet[-200:])
        check("nested quote >> round-trips from encrypted store",
              "> outer level\n>> inner level" in snippet, snippet[-300:])
        # 用户手册 3.5 lists 无序/有序列表 among the editor-supported
        # constructs ("编辑器支持：… 无序/有序列表 …"). Preview checks above
        # already render list markers, but no check ever drove a list through
        # save -> encrypt -> decrypt -> reload: list marker lines fall into
        # plain paragraph blocks in textToBlocks (src/factory.js) and re-emit
        # verbatim through blocksToMarkdown, so `- item` / `1. item` must
        # survive the encrypted store byte-for-byte like tables/fences/quotes.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(
            "\n- first bullet\n- second bullet\n"
            "1. one\n2. two\n3. three"
        )
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")[0].click()
        time.sleep(1.0)
        snippet = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("unordered list markers round-trip from encrypted store (manual 3.5)",
              "- first bullet" in snippet and "- second bullet" in snippet,
              snippet[-300:])
        check("ordered list markers round-trip from encrypted store (manual 3.5)",
              "1. one" in snippet and "2. two" in snippet and "3. three" in snippet,
              snippet[-300:])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        inner = driver.find_element(By.ID, "preview").get_attribute("innerHTML")
        check("preview renders the round-tripped lists as <ul>/<ol> (manual 3.5)",
              "<ul><li>first bullet</li><li>second bullet</li></ul>" in inner
              and "<ol><li>one</li><li>two</li><li>three</li></ol>" in inner,
              inner[-300:])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.4)
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        inner = driver.find_element(By.ID, "preview").get_attribute("innerHTML")
        check("preview renders every heading level 1..6",
              all(f"<h{n}>H{n} level</h{n}>" in inner for n in range(1, 7)),
              inner[-400:])
        check("preview renders the nested quotes as blockquotes (manual 3.5)",
              "<blockquote>outer level</blockquote>" in inner
              and "<blockquote>&gt; inner level</blockquote>" in inner, inner[-400:])
        check("preview renders the tilde fence as a code block (manual 3.5)",
              "<pre><code>print('tilde fence')</code></pre>" in inner
              and "~~~" not in inner, inner[-400:])
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.4)

        # ---- 用户手册 3.1: workspace name is *public metadata* stored in
        # cleartext alongside the salt ("以明文形式作为公开元数据保存（与盐值
        # 同级，不含任何文档内容），并参与密钥派生"). Two things the "surfaced
        # in app header" check above does not prove: (a) the raw IndexedDB
        # workspace record (id=1) really holds the name as plaintext next to
        # the public salt — with exactly the public-metadata keys and zero
        # document content (the current doc carries the known plaintext
        # markers, so their absence from the record is meaningful); (b) the
        # name actually *participates* in key derivation — same password +
        # salt, different workspace name ⇒ a different derived workspace key
        # (HKDF namespace), which is why machines must agree on the name
        # before they can share a workspace.
        ws_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const req = indexedDB.open('local-kb');
            const db = await new Promise((res) => { req.onsuccess = () => res(req.result); req.onerror = () => res(null); });
            if (!db) return cb(null);
            const tx = db.transaction('workspace', 'readonly');
            const q = tx.objectStore('workspace').get(1);
            q.onsuccess = () => { db.close(); cb(JSON.stringify(q.result)); };
            q.onerror = () => { db.close(); cb(null); };
          })().catch(() => cb(null));
        """)
        ws_rec = json.loads(ws_json) if ws_json else {}
        check("manual 3.1: workspace name stored in cleartext public metadata",
              ws_rec.get("name") == "ops-vault", ws_rec.get("name"))
        check("manual 3.1: name stored beside the public salt + crypto params",
              bool(ws_rec.get("salt")) and ws_rec.get("iterations") == 310000
              and isinstance(ws_rec.get("verifier"), dict), sorted(ws_rec.keys()))
        ws_str = json.dumps(ws_rec)
        check("manual 3.1: workspace record carries no document content",
              all(n not in ws_str for n in
                  [KNOWN_TITLE, "oscar-7", "48.8584N", "deflector", "Kyoto"]),
              ws_str[:160])
        deriv_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            try {
              const req = indexedDB.open('local-kb');
              const db = await new Promise((res) => { req.onsuccess = () => res(req.result); req.onerror = () => res(null); });
              const tx = db.transaction('workspace', 'readonly');
              const q = tx.objectStore('workspace').get(1);
              const rec = await new Promise((res) => { q.onsuccess = () => res(q.result); q.onerror = () => res(null); });
              db.close();
              if (!rec) return cb("{}");
              const { deriveKeyBytes, deriveWorkspaceKey } = await import('/src/crypto.js');
              const pw = "correct horse battery staple";
              const uk = new Uint8Array(await deriveKeyBytes(pw, rec.salt, rec.iterations));
              const wk0 = new Uint8Array(await deriveWorkspaceKey(uk, rec.name));
              const wk1 = new Uint8Array(await deriveWorkspaceKey(uk, rec.name + "x"));
              const b64 = (b) => { let s = ""; for (const x of b) s += String.fromCharCode(x); return btoa(s); };
              cb(JSON.stringify({ wsKey: b64(wk0), alt: b64(wk1), differs: b64(wk0) !== b64(wk1) }));
            } catch (e) { cb(JSON.stringify({ error: String(e) })); }
          })();
        """)
        deriv = json.loads(deriv_json) if deriv_json else {}
        check("manual 3.1: workspace name participates in key derivation (HKDF)",
              deriv.get("differs") is True, deriv)

        # ---- AC2: IndexedDB holds no plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [KNOWN_TITLE, "oscar-7", "48.8584N", "deflector", "Project Snowstorm", "Kyoto", "Osaka"]:
            check(f"AC2: no plaintext ('{needle}') in IndexedDB", needle not in dump)
        docs_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const r = indexedDB.open('local-kb');
            const db = await new Promise((res) => {
              r.onsuccess = () => res(r.result);
              r.onerror = () => res(null);
            });
            if (!db) return cb('[]');
            const tx = db.transaction('docs', 'readonly');
            const q = tx.objectStore('docs').getAll();
            q.onsuccess = () => { db.close(); cb(JSON.stringify(q.result)); };
            q.onerror = () => { db.close(); cb('[]'); };
          })().catch((e) => cb('[]'));
        """)
        check("AC2: IndexedDB contains ciphertext records", "dekData" in docs_json)

        # ---- AC5: FTS search finds the doc
        search = driver.find_element(By.ID, "search-term")
        search.send_keys("oscar")
        # 用户手册 3.4: "实时列出命中文档" — typing alone (no Go click) must
        # already narrow the list after the input debounce fires.
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: typing in the box searches live (no Go click)",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        search.clear()
        time.sleep(1.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: clearing the box restores the full doc list",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        search.send_keys("oscar")
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: search hits the doc by keyword",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])

        # ---- AC5: 用户手册 3.4 "系统对标题、正文、代码、引用与表格单元格进行
        # 全文匹配" — the FTS index covers every block type (built from the
        # store's plaintextForIndex: title + each block's text). Each query is
        # issued against #search-term directly (250ms debounce + Go click),
        # with a clear between queries so hits belong to the current term.
        def search_hits(term):
            search.clear()
            search.send_keys(term)
            driver.find_element(By.ID, "btn-search").click()
            time.sleep(1.2)
            return [r.text or "" for r in
                    driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")]

        rows = search_hits("const")
        check("AC5: search matches fenced code bodies",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("nested")
        check("AC5: search matches quote bodies",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # 用户手册 3.4: "系统对标题、正文、代码、引用与表格单元格进行全文匹配"
        # — the index must cover the *title* as well as every block type. The
        # body above holds code/quote/table tokens; this pins the remaining
        # field with a token that exists ONLY in the title
        # ("Project Snowstorm Operations" — "snowstorm" never appears in the
        # body, so a hit proves the title is what matched, not a body word).
        rows = search_hits("snowstorm")
        check("AC5: search matches the doc by its title word (manual 3.4)",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("Kyoto")
        check("AC5: search matches table cells",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # 用户手册 3.4 技巧: "关键词可连续用空格分隔" — FTS5 treats the two
        # terms as an implicit AND, so only a doc holding both matches.
        rows = search_hits("Kyoto Japan")
        check("AC5: space-separated keywords AND-match (table cells)",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("const oscar")
        check("AC5: space-separated keywords AND-match (code + body)",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("zanzibarbazooka")
        check("AC5: search returns nothing for an absent token",
              not any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # 用户手册 5.1 故障 4: "检查检索词是否包含超出分词规则的符号" — a
        # hyphenated token must still hit (the body holds "access code oscar-7"),
        # and a punctuation-only query must degrade to an empty list rather than
        # break the page (unit-level FTS-hardening pins the same on SQLite/FTS5:
        # "a valid query still works afterwards"). Search must stay functional.
        rows = search_hits("oscar-7")
        check("AC5: a hyphenated token (oscar-7) still matches",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits('"-*(')
        check("AC5: a punctuation-only query yields no hits without crashing",
              not any(KNOWN_TITLE in (t or "") for t in rows), rows)
        rows = search_hits("oscar")
        check("AC5: search still works after a punctuation-only query",
              any(KNOWN_TITLE in (t or "") for t in rows), rows)
        # Restore the full list deterministically: an empty-query Go click runs
        # doSearch -> refreshDocList (the debounced input handler alone leaves
        # the previous "No matches." list in place until it fires). The import
        # refusal checks below depend on the doc being visible in #doc-list.
        search.clear()
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: clearing the box after a no-match restores the doc list",
              any(KNOWN_TITLE in (t.text or "") for t in rows), rows)

        # ---- P1 doc switching: clicking another existing doc while the current
        # one has unsaved edits must confirm (用户手册 3.3: "未保存的修改在切换
        # 文档或离开页面时会收到提醒"). The new-doc guard pins the "new doc"
        # path; this pins the third path — the doc-list row click. By now the
        # table/heading/quote/fence content is persisted (the blocks above
        # saved it), so the throwaway switch-target doc only needs a clean
        # create; its raw removal below restores the exactly-one-doc vault all
        # downstream sections (AC4, import, M3 sync, driver2 restore) assume.
        # By now every edit above is persisted, so the editor is clean. Type
        # into the main doc to mark it dirty — the new-doc guard (pinned by the
        # block above) must still fire, and accepting discards only this
        # throwaway marker.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nSWITCH-LEADIN-MARKER")
        time.sleep(0.3)
        driver.find_element(By.ID, "btn-new").click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        check("switch-doc: new-doc prompt still active on the dirty editor", True)
        alert.accept()  # create the throwaway doc to switch to
        time.sleep(1.0)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "btn-save")), 12)
        target_title = "SWITCH-TARGET-DOC"
        title = driver.find_element(By.ID, "editor-title")
        title.clear()
        title.send_keys(target_title)
        area = driver.find_element(By.ID, "editor-area")
        area.clear()
        area.send_keys("throwaway switch target body")
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)

        def row_by_text(text):
            for r in driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row"):
                if (r.text or "").strip() == text:
                    return r
            return None

        main_row = row_by_text(KNOWN_TITLE)
        check("switch-doc: main doc row present alongside the target",
              main_row is not None and row_by_text(target_title) is not None,
              [r.text for r in driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")])
        main_row.click()  # clean switch: editor was just saved, not dirty
        time.sleep(1.0)
        check("switch-doc: clean switch opens the main doc",
              KNOWN_TITLE in driver.find_element(By.ID, "editor-title").get_attribute("value"))

        # Make the main doc dirty, then try to switch to the target doc: the
        # confirm must appear; dismissing stays on the main doc with the edits.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys("\nSWITCH-DIRTY-PROOF")
        time.sleep(0.3)
        check("switch-doc: typing marks the doc dirty",
              "edited" in driver.find_element(By.ID, "doc-meta").text.lower())
        row_by_text(target_title).click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        try:
            alert_text = alert.text or ""
        except Exception:
            alert_text = ""
        check("switching docs prompts to discard unsaved edits (manual 3.3 pin)",
              "Discard unsaved changes" in alert_text, alert_text)
        alert.dismiss()
        time.sleep(0.3)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("switch-doc: dismissing stays on the main doc with edits",
              KNOWN_TITLE in title_val and "SWITCH-DIRTY-PROOF" in area_val,
              (title_val, area_val[-120:]))

        # Accepting the discard really switches documents.
        row_by_text(target_title).click()
        time.sleep(0.6)
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(1.0)
        check("switch-doc: accepting switches to the target doc",
              target_title in driver.find_element(By.ID, "editor-title").get_attribute("value"))

        # The throwaway doc must not survive this block: downstream checks
        # depend on an exactly-one-doc vault (M3 sync receipt "0 docs synced
        # in, 1 skipped" and the final delete leaving zero rows). Remove it via
        # raw IndexedDB (deleteDoc, no tombstone) — a UI delete would write a
        # tombstone into the meta store that then rides the sync tape.
        cleanup = driver.execute_async_script("""
            const cb = arguments[arguments.length - 1];
            (async () => {
              const { IndexedDBBackend } = await import('/idb.js');
              const { openKb } = await import('/src/factory.js');
              const db = new IndexedDBBackend();
              const r = await openKb(db, 'correct horse battery staple');
              if (!r.ok) return cb({ error: 'unlock failed', titles: [] });
              const docs = await r.store.list();
              const hit = docs.find((d) => d.title === 'SWITCH-TARGET-DOC');
              if (hit) {
                await db.deleteDoc(hit.id);
                await db.delMeta('tombstone:' + hit.id);
              }
              return cb({ removed: hit ? hit.id : null,
                          titles: docs.map((d) => d.title) });
            })().catch((e) => cb({ error: String(e), titles: [] }));
        """)
        check("switch-doc: throwaway doc was pruned",
              cleanup.get("removed") is not None, cleanup)
        # Reload so the doc list and FTS index rebuild from the pruned store,
        # then prove the vault is back to exactly the one main doc.
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("switch-doc: vault is back to exactly one doc",
              len(rows) == 1 and KNOWN_TITLE in (rows[0].text or ""),
              [r.text for r in rows])
        rows[0].click()
        time.sleep(1.0)
        check("switch-doc: unsaved edit was never persisted",
              "SWITCH-DIRTY-PROOF" not in driver.find_element(By.ID, "editor-area").get_attribute("value"))

        # ---- AC4: .kb export - download and inspect (ciphertext only)
        # Read the export payload straight from page JS (same serialization as
        # the browser download; avoids post-launch download-dir configuration).
        export_json = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { exportKB } = await import('/src/bak.js');
            const { openKb } = await import('/src/factory.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb('{"error":"unlock"}');
            const rows = [];
            for (const {id} of await db.listDocs()) rows.push(await db.getDoc(id));
            cb(JSON.stringify(exportKB(await db.getParams(), rows)));
          })().catch((e) => cb(JSON.stringify({ error: String(e) })));
        """)
        export_obj = json.loads(export_json)
        check("AC4: .kb export contains a workspace", export_obj.get("format") == 1)
        check("AC4: .kb export is ciphertext (no plaintext)",
              all(needle not in export_json for needle in
                  [KNOWN_TITLE, "oscar-7", "48.8584N", "Project Snowstorm"]))

        # ---- AC4: .kb carries the crypto parameters claimed by 用户手册 4.1
        # (PBKDF2 迭代次数 310,000; AES-256-GCM 12B IV) and 4.2 (每文档 DEK 由
        # 工作区密钥 AES-256-GCM 封装后与文档密文同存). The workspace params are
        # intentionally cleartext (public nonces/strength), never the password.
        def _b64_bytes(s):
            import base64 as _b64
            pad = "=" * (-len(s) % 4)
            return _b64.urlsafe_b64decode(s + pad)

        ws = export_obj.get("workspace") or {}
        check("AC4: workspaces store PBKDF2 iterations = 310000",
              ws.get("iterations") == 310000, ws.get("iterations"))
        check("AC4: workspace salt is 16 random bytes",
              len(_b64_bytes(ws.get("salt", ""))) == 16, ws.get("salt"))
        ver = ws.get("verifier") or {}
        check("AC4: unlock verifier iv is a 12-byte AES-GCM IV",
              len(_b64_bytes(ver.get("iv", ""))) == 12, ver.get("iv"))
        for d in export_obj.get("documents", []):
            check(f"AC4: doc {str(d.get('id'))[:8]} has a wrapped DEK (dekIv)",
                  len(_b64_bytes(d.get("dekIv", ""))) == 12, d.get("dekIv"))
            check(f"AC4: doc {str(d.get('id'))[:8]} ciphertext iv is 12 bytes",
                  len(_b64_bytes(d.get("iv", ""))) == 12 and d.get("data"), d.get("iv"))
        check("AC4: deleted docs do not reappear as .kb documents",
              all(not str(d.get("id", "")).startswith("tombstone:")
                  for d in export_obj.get("documents", [])))

        # ---- AC4: the real Export .kb button downloads a timestamp-stamped
        # filename (用户手册 3.6: "浏览器下载仅含密文的备份文件（文件名含导出
        # 时间）"). Capturing the click with a patched anchor avoids writing a
        # real download to disk; the value under test is stampedName()'s
        # `local-kb-backup-<UTC YYYY-MM-DD HH MM SS>.kb` pattern, which the
        # restored file needs to be distinguishable from other backups.
        before = datetime.now(timezone.utc).replace(microsecond=0)
        captured = driver.execute_script("""
          window.__captured = null;
          const orig = HTMLAnchorElement.prototype.click;
          HTMLAnchorElement.prototype.click = function () {
            if (this.download) window.__captured = this.download;
            orig.call(this);
          };
          document.getElementById('btn-export').click();
          return true;
        """)
        name = None
        # exportKBFile is async (public/main.js:215): it awaits listDocs()
        # before creating and clicking the download anchor, so the capture
        # lands on a later JS turn. Poll up to 5s instead of reading once.
        for _ in range(50):
            name = driver.execute_script("return window.__captured")
            if name:
                break
            time.sleep(0.1)
        after = datetime.now(timezone.utc).replace(microsecond=0)
        time.sleep(0.5)
        stamp_re = r"^local-kb-backup-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.kb$"
        check("AC4: .kb export filename embeds the export timestamp",
              bool(re.match(stamp_re, name or "")), name)
        if name:
            stamp = re.match(stamp_re, name).group(0) \
                .replace("local-kb-backup-", "").replace(".kb", "")
            ts = datetime.strptime(stamp, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=timezone.utc)
            check("AC4: .kb export filename timestamp is current (within the run window)",
                  before <= ts <= after, f"ts={ts} before={before} after={after}")

        # ---- unlocked-state .kb import refusal (用户手册 3.7 场景二 / 5.1
        # 故障 3): importing a foreign-workspace or malformed backup while in
        # the app must refuse loudly in the log instead of surfacing as an
        # unhandled rejection with no message — and must leave the vault intact.
        foreign = json.loads(export_json)
        # A different workspace salt → different derived workspace key.
        foreign["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
        foreign_bytes = json.dumps(foreign).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'foreign.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(foreign_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm; the salt gate then refuses
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("import: foreign-workspace .kb refused with a message",
              "Import refused" in log and "different workspace" in log, log)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: refused .kb left the vault untouched",
              any(KNOWN_TITLE in (d.text or "") for d in docs),
              [d.text for d in docs])
        # malformed .kb (valid local-kb envelope, missing workspace) must be
        # refused at importKB, before any write, with a surfaced message
        broken_bytes = json.dumps({"app": "local-kb", "format": 1}).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'broken.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(broken_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm; the shape check then refuses
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("import: malformed .kb refused with a message",
              "Import refused" in log and "broken" in log, log)

        # ---- unlocked-state .kb import merge (用户手册 3.7 场景二): re-importing
        # a same-workspace backup in the app must merge idempotently — confirm
        # asked, log count matches the payload, existing ids skipped (幂等导入),
        # no duplicate rows. This is the positive branch of the two refusal
        # tests above, driven through the same real #import-file path.
        same_kb_bytes = export_json.encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(same_kb_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()  # confirm the merge
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: same-workspace .kb merge logged the payload count",
              "Imported 1 docs." in log, log)
        check("import: merged vault still holds exactly the original doc",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])
        # second identical import: same-salt backup, existing id → duplicate
        # skip (importKB 幂等导入) — the vault must not grow a second row
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same2.kb',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('import-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(same_kb_bytes))
        wait_for(driver, EC.alert_is_present(), 6)
        driver.switch_to.alert.accept()
        time.sleep(1.2)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("import: re-import of the same backup is idempotent (no duplicates)",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # ---- M3 sync: build a .kbsync tape, apply it to a fresh peer, verify
        # the peer converges and the carried tape is ciphertext-only.
        sync_res = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, serializeTape, applyTapeFile } = await import('/src/syncfile.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb({ error: 'unlock' });
            const tape = await buildTape(r.params, db);
            const txt = new TextDecoder().decode(serializeTape(tape));
            // Fresh peer sharing the same workspace params.
            const peer = new IndexedDBBackend('local-kb-peer');
            await peer.saveParams(r.params);
            const pr = await openKb(peer, 'correct horse battery staple');
            if (!pr.ok) return cb({ error: 'peer-unlock' });
            const res = await applyTapeFile(peer, tape);
            const ids = (await peer.listDocs()).map((d) => d.id);
            return cb({ tapeJson: txt, applied: res.applied, peerDocs: ids.length });
          })().catch((e) => cb({ error: String(e) }));
        """)
        sync_obj = json.loads(json.dumps(sync_res))
        check("M3: peer converged via sync tape", sync_obj.get("applied", -1) > 0 and sync_obj.get("peerDocs", 0) > 0)

        tape_json = sync_obj.get("tapeJson") or ""
        check("M3: sync tape is ciphertext (no plaintext)",
              all(x not in tape_json for x in [KNOWN_TITLE, "oscar-7", "48.8584N", "Project Snowstorm"]))
        ids_json = json.dumps(sync_obj.get("peerDocs", 0))
        check("M3: sync tape carried a doc (peer lists " + ids_json + ")", sync_obj.get("peerDocs", 0) >= 1)

        # ---- 用户手册 3.8 real-button sync flow: the app's Sync out button must
        # download a timestamp-stamped .kbsync (同 .kb 的 stampedName 命名规则),
        # the Sync in file input must apply a same-workspace tape with the app's
        # own "N docs synced in, M skipped." receipt (LWW idempotence: this vault
        # already holds that exact version → skipped, vault unchanged), and a
        # foreign-workspace tape must be refused by the workspace gate in the log
        # ("Sync refused: ... different workspace") with the vault untouched.
        # The export block above installed the anchor click hook, so the real
        # button click is captured into window.__captured without a disk write.
        sync_before = datetime.now(timezone.utc).replace(microsecond=0)
        driver.execute_script("""
          window.__captured = null;
          document.getElementById('btn-sync-out').click();
          return true;
        """)
        # syncOutFile is async (public/main.js:264): it awaits buildTape()
        # before creating and clicking the download anchor, so the capture
        # lands on a later JS turn. Poll up to 5s for it instead of reading
        # once, or this check races the handler (flaky "captured None").
        sync_name = None
        for _ in range(50):
            sync_name = driver.execute_script("return window.__captured")
            if sync_name:
                break
            time.sleep(0.1)
        sync_after = datetime.now(timezone.utc).replace(microsecond=0)
        time.sleep(0.5)
        sync_stamp_re = r"^local-kb-sync-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.kbsync$"
        check("sync: Sync out downloads a timestamp-stamped .kbsync name",
              bool(re.match(sync_stamp_re, sync_name or "")), sync_name)
        if sync_name:
            sync_stamp = re.match(sync_stamp_re, sync_name).group(0) \
                .replace("local-kb-sync-", "").replace(".kbsync", "")
            sync_ts = datetime.strptime(sync_stamp, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=timezone.utc)
            check("sync: .kbsync filename timestamp is current (within the run window)",
                  sync_before <= sync_ts <= sync_after,
                  f"ts={sync_ts} before={sync_before} after={sync_after}")
        log = driver.find_element(By.ID, "log").text
        check("sync: Sync out logs the ciphertext-only tape receipt",
              "Wrote sync tape" in log and "ciphertext only" in log, log)

        # Reuse the app-level Sync in input with a tape freshly built from THIS
        # vault: same workspace, same exact updatedAt → the LWW no-op branch
        # (skipped) — the receipt must read "0 docs synced in, 1 skipped." and
        # the doc list must stay at exactly one doc (the delete block depends
        # on that count).
        fresh_tape = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, serializeTape } = await import('/src/syncfile.js');
            const db = new IndexedDBBackend();
            const r = await openKb(db, 'correct horse battery staple');
            if (!r.ok) return cb('{"error":"unlock"}');
            const tape = await buildTape(r.params, db);
            cb(new TextDecoder().decode(serializeTape(tape)));
          })().catch((e) => cb('{"error":"' + String(e) + '"}'));
        """)
        fresh_bytes = fresh_tape.encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'same.kbsync',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('sync-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(fresh_bytes))
        time.sleep(2.0)  # apply + openKb re-derive (PBKDF2) + refreshDocList
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("sync: same-workspace Sync in logs the skip receipt",
              "0 docs synced in, 1 skipped." in log, log)
        check("sync: same-workspace Sync in left the vault at exactly one doc",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # A .kbsync from a different workspace (tampered salt → different derived
        # workspace key) must be refused at the workspace gate without touching
        # the vault — the "Sync refused" message is the app-level error channel.
        foreign_tape = json.loads(fresh_tape)
        foreign_tape["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
        foreign_bytes = json.dumps(foreign_tape).encode("utf-8")
        driver.execute_script("""
          const f = new File([new Uint8Array(arguments[0])], 'foreign.kbsync',
                             { type: 'application/json' });
          const dt = new DataTransfer();
          dt.items.add(f);
          const inp = document.getElementById('sync-file');
          inp.files = dt.files;
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        """, list(foreign_bytes))
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("sync: foreign-workspace tape refused by the workspace gate",
              "Sync refused" in log and "different workspace" in log, log)
        check("sync: refused tape left the vault untouched",
              len(docs) == 1 and KNOWN_TITLE in (docs[0].text or ""),
              [d.text for d in docs])

        # ---- 用户手册 3.8 LWW convergence, driven fully in-page on scratch
        # peers so the main vault stays untouched (later blocks depend on
        # exactly one doc). Peer A seeds this vault's tape (the OLD version),
        # then edits the doc; the fresh updatedAt stamped after the PBKDF2
        # unlock makes tape B strictly newer. A fresh peer that receives
        # old-then-new must converge to the newest version; a fresh peer that
        # receives new-then-old must skip the stale tape (newest updatedAt
        # wins); reapplying the newest tape must be a no-op (idempotence).
        lww = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, applyTapeFile } = await import('/src/syncfile.js');
            const PW = 'correct horse battery staple';
            const MARK = 'LWW-NEWER-PROOF-7d0e';
            const hasMarker = (d) => !!d && (d.blocks || []).some((b) => (b.text || '').includes(MARK));
            // Tape A = this vault's current state (the OLD version).
            const main = new IndexedDBBackend();
            const r = await openKb(main, PW);
            if (!r.ok) return cb({ error: 'unlock' });
            const docs = await r.store.list();
            const docId = docs[0].id;
            const tapeA = await buildTape(r.params, main);
            const mkPeer = async (name) => {
              const p = new IndexedDBBackend(name);
              await p.saveParams(r.params);
              const pr = await openKb(p, PW);
              return { p, pr };
            };
            // Peer A: seed the OLD tape, then edit the doc. Unlocking re-runs
            // PBKDF2, so nowISO() in store.edit stamps a strictly later
            // updatedAt than anything tape A carries.
            const a = await mkPeer('local-kb-peer-lww-a');
            if (!a.pr.ok) return cb({ error: 'peer-a-unlock' });
            const s1 = await applyTapeFile(a.p, tapeA);
            const before = await a.pr.store.get(docId);
            const newTitle = (before.title || '') + ' (LWW)';
            const newBlocks = (before.blocks || []).concat({ type: 'paragraph', text: MARK });
            await a.pr.store.edit(docId, newTitle, newBlocks);
            const tapeB = await buildTape(r.params, a.p);
            const oldTs = (tapeA.entries.find((e) => e.id === docId) || {}).updatedAt || '';
            const newTs = (tapeB.entries.find((e) => e.id === docId) || {}).updatedAt || '';
            // Peer B: old tape, then new tape -> newest wins (applied>0).
            const b = await mkPeer('local-kb-peer-lww-b');
            const r1 = await applyTapeFile(b.p, tapeA);
            const r2 = await applyTapeFile(b.p, tapeB);
            const db_ = await b.pr.store.get(docId);
            // Peer C: new tape, then old tape -> stale tape skipped (applied=0).
            const c = await mkPeer('local-kb-peer-lww-c');
            const q1 = await applyTapeFile(c.p, tapeB);
            const q2 = await applyTapeFile(c.p, tapeA);
            const dc = await c.pr.store.get(docId);
            // Reapplying the newest tape on C is a no-op (skipped).
            const q3 = await applyTapeFile(c.p, tapeB);
            const dc2 = await c.pr.store.get(docId);
            return cb({
              docId, oldTs, newTs,
              bNewWins: r2.applied >= 1 && !!db_ && hasMarker(db_),
              cStaleSkipped: q2.applied === 0,
              cKeptNewer: q1.applied >= 1 && hasMarker(dc),
              cReapplySkipped: q3.applied === 0 && hasMarker(dc2),
            });
          })().catch((e) => cb({ error: String(e) }));
        """)
        l = json.loads(json.dumps(lww))
        check("sync: LWW newest-wins (manual 3.8) — newer tape applied over the older version",
              l.get("bNewWins") is True and bool(l.get("oldTs")) and l.get("newTs", "") > l.get("oldTs", ""),
              "oldTs=%s newTs=%s bNewWins=%s" % (l.get("oldTs"), l.get("newTs"), l.get("bNewWins")))
        check("sync: LWW (manual 3.8) — stale tape cannot clobber the newer version",
              l.get("cStaleSkipped") is True and l.get("cKeptNewer") is True,
              "kept-newer=%s stale-applied-zero=%s" % (l.get("cKeptNewer"), l.get("cStaleSkipped")))
        check("sync: LWW (manual 3.8) — reapplying the newest tape is idempotent",
              l.get("cReapplySkipped") is True,
              "reapply-applied-zero=%s" % (l.get("cReapplySkipped"),))

        # ---- 用户手册 3.8 "删除以墓碑形式传播": a tombstone must ride a
        # .kbsync tape to a fresh peer and there delete the doc, then keep that
        # peer from ever resurrecting it via a stale live tape. Driven in-page on
        # scratch peers (main vault untouched). Peer A holds the master doc and
        # edits it so peer B's "decoy" tape carries the doc; A then deletes the
        # doc via the encrypted store (tombstone recorded at the deleted
        # updatedAt) and exports the post-delete tape, which must carry only a
        # deleted:true entry with NO ciphertext / title.
        tomb = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { openKb } = await import('/src/factory.js');
            const { buildTape, applyTapeFile, serializeTape } = await import('/src/syncfile.js');
            const PW = 'correct horse battery staple';
            const main = new IndexedDBBackend();
            const r = await openKb(main, PW);
            if (!r.ok) return cb({ error: 'unlock' });
            const docs = await r.store.list();
            const docId = docs[0].id;
            const mkPeer = async (name) => {
              const p = new IndexedDBBackend(name);
              await p.saveParams(r.params);
              const pr = await openKb(p, PW);
              return { p, pr };
            };
            const a = await mkPeer('local-kb-peer-tomb-a');
            const seed = await applyTapeFile(a.p, await buildTape(r.params, main));
            const doc = await a.pr.store.get(docId);
            const title = (doc.title || '') + ' (Tomb)';
            const blocks = (doc.blocks || []).concat({ type: 'paragraph', text: 'TOMBSTONE-PROP-PROOF-1cb9' });
            // Store edit stamps a fresh updatedAt; guaranteed strictly newer than
            // the seeded version because the PBKDF2 unlock took ~1s.
            await a.pr.store.edit(docId, title, blocks);
            const decoyTape = await buildTape(r.params, a.p);       // live doc, newer than seed
            // Delete the doc on A: DocStore.delete leaves a tombstone at the
            // deleted doc's updatedAt in the meta store.
            await a.pr.store.delete(docId);
            const delTape = await buildTape(r.params, a.p);          // tombstone only
            const delEntry = (delTape.entries || []).find((e) => e.id === docId);
            const delJson = new TextDecoder().decode(serializeTape(delTape));
            // Peer B receives ONLY the post-delete tape -> tombstone carries the
            // delete and the doc is absent, yet the tombstone is recorded.
            const b = await mkPeer('local-kb-peer-tomb-b');
            const rb = await applyTapeFile(b.p, delTape);
            const bHas = await b.pr.store.list();
            const bLive = bHas.find((d) => d.id === docId);
            const bTombs = (await b.p.listTombstones()).filter((t) => t.id === docId);
            // Stale live tape against the tombstone on B: skipped, doc stays gone.
            const rc = await applyTapeFile(b.p, decoyTape);
            const cLive = await b.pr.store.get(docId);
            return cb({
              docId,
              delApplied: rb.applied,
              bDocGone: !bLive && bTombs.length >= 1,
              tombEncOnly: delEntry && delEntry.deleted === true
                           && !('data' in delEntry) && !('iv' in delEntry)
                           && !delJson.includes('TOMBSTONE-PROP-PROOF-1cb9'),
              decoySkipped: rc.applied === 0 && !cLive,
            });
          })().catch((e) => cb({ error: String(e) }));
        """)
        t = json.loads(json.dumps(tomb))
        check("sync: tombstone rides the tape to a fresh peer (manual 3.8)",
              t.get("delApplied") == 1 and t.get("bDocGone") is True,
              "applied=%s docGone=%s" % (t.get("delApplied"), t.get("bDocGone")))
        check("sync: tape tombstone carries no ciphertext/plaintext (manual 3.8)",
              t.get("tombEncOnly") is True,
              "tombEncOnly=%s" % (t.get("tombEncOnly"),))
        check("sync: stale live tape cannot resurrect the tombstoned doc (manual 3.8)",
              t.get("decoySkipped") is True,
              "decoySkipped=%s" % (t.get("decoySkipped"),))

        # ---- lock-screen .kb restore onto a fresh machine (用户手册 3.7,
        # 登记表 功能4, SPEC AC4): a brand-new browser profile has no vault to
        # unlock and no way to reach the in-app Import .kb (view-app only), so
        # the lock screen must accept the exported backup directly, create the
        # vault from the backup's params and unlock with the backup password.
        # A second headless profile acts as the fresh machine; the first
        # driver's IndexedDB must stay untouched.
        kb_bytes = export_json.encode("utf-8")
        # A second geckodriver session gets its own isolated temp profile
        # (same launch pattern as the main driver), so this browser has no
        # vault at all — the fresh-machine premise of the restore path.
        opts2 = Options()
        opts2.headless = True
        opts2.binary_location = fb
        opts2.set_preference("dom.indexedDB.enabled", True)
        driver2 = webdriver.Firefox(options=opts2)
        try:
            driver2.get(BASE + "/")
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            check("restore: fresh profile shows the lock screen", True)
            # A malformed envelope that still passes deserializeKB (app id set but
            # no workspace / no documents) must be refused with a message, not
            # crash restoreKBFile on kb.workspace.salt / kb.documents.length —
            # and must leave the fresh profile vault-less for the real restore.
            broken_bytes = json.dumps({"app": "local-kb", "format": 1}).encode("utf-8")
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'broken.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(broken_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            time.sleep(1.2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: malformed .kb refused cleanly on the lock screen",
                  "Restore refused" in log2 and "broken" in log2, log2)
            check("restore: refused .kb left the fresh profile locked",
                  driver2.find_element(By.ID, "view-unlock").is_displayed())
            # hand the .kb backup to the hidden file input the same way a real
            # <input type=file> change event does
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'backup.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            rpw = driver2.find_element(By.ID, "restore-pw")
            rpw.clear()  # the broken attempt above already typed a password
            rpw.send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: create the vault from the backup
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(driver2, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
            rows = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("restore: backup doc appears in the fresh vault",
                  any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: log confirms the restore by document count",
                  "Restored .kb backup" in log2, log2)

            # ---- lock-screen restore refusal for a *different* workspace
            # (用户手册 3.7 场景一: "若当前浏览器已存在同一工作区（盐值相同）则会先
            # 确认再合并导入；若已存在其他工作区（盐值不同）则恢复会被拒绝" and
            # 5.1 故障 3). driver2 now holds the restored (same-salt) vault;
            # refresh it back to the lock screen, hand it a foreign-salt .kb,
            # and the lock-screen precheck (main.js restoreKBFile) must refuse
            # BEFORE any confirm()/unlock — the existing vault stays restorable.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            foreign2 = json.loads(export_json)
            # a different workspace salt → different derived workspace key
            foreign2["workspace"]["salt"] = "AAAAAAAAAAAAAAAAAAAAAA"
            foreign2_bytes = json.dumps(foreign2).encode("utf-8")
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'foreign-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(foreign2_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            # no confirm() is offered for a foreign salt — the precheck returns
            # before the merge/create-vault paths can arm one
            time.sleep(1.2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: foreign-workspace .kb refused on the lock screen (manual 3.7/故障 3)",
                  "Restore refused" in log2 and "different workspace" in log2, log2)
            check("restore: refused restore left the profile on the lock screen",
                  driver2.find_element(By.ID, "view-unlock").is_displayed())
            # the existing same-salt vault is untouched: unlock and the doc returns
            driver2.find_element(By.ID, "pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-unlock").click()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(driver2, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("restore: refused restore left the existing vault intact",
                  any(KNOWN_TITLE in (r.text or "") for r in rows2), [r.text for r in rows2])

            # ---- lock-screen restore merge for the *same* workspace
            # (用户手册 3.7 场景一: "若当前浏览器已存在同一工作区（盐值相同）则会先
            # 确认再合并导入" — the positive branch: lock the existing vault back,
            # feed it the *unmodified* backup of the same workspace (byte-identical
            # salt, so the derived workspace key matches), and the lock screen must
            # arm the merge confirm, then merge without duplicating the doc.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'same-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-restore").click()
            # a same-salt backup over an existing vault arms the merge confirm —
            # unlike the foreign-salt refusal above, this pauses for the user
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: "…Merge the backup into it?"
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            mount_rows = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            t2 = [r.text for r in mount_rows]
            check("restore: same-salt backup merges at the lock screen confirm (manual 3.7 场景一)",
                  len(mount_rows) == 1 and KNOWN_TITLE in (t2[0] or ""), t2)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: merged log confirms the restore by document count",
                  "Restored .kb backup" in log2, log2)

            # ---- wrong master password on the lock-screen restore stays
            # non-destructive (用户手册 3.2: "密码错误将被校验器直接拒绝，不会产生
            # 任何可读数据，也不会破坏现有数据"; 5.3 Q1: "主密码无法找回"). The
            # same-salt backup merges fine, but re-unlocking with the *typed*
            # password must fail at the verifier, leaving the profile on the lock
            # screen; the existing vault then still unlocks with the real password.
            driver2.refresh()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'wrongpw-restore.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('restore-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            driver2.find_element(By.ID, "restore-pw").send_keys("definitely the wrong password")
            driver2.find_element(By.ID, "btn-restore").click()
            # same-salt backup over the existing vault -> merge confirm fires
            wait_for(driver2, EC.alert_is_present(), 6)
            driver2.switch_to.alert.accept()  # confirm: "…Merge the backup into it?"
            # importKB merges idempotently, then openKb hits the verifier with the
            # wrong password and fails -> the lock screen must stay up. The failed
            # branch never switches views, so wait on the log message itself (the
            # async restore path has not finished until the refusal line appears).
            wait_for(driver2, lambda d: "not accepted" in (
                d.find_element(By.ID, "log").text or ""), 12)
            log2 = driver2.find_element(By.ID, "log").text
            check("restore: wrong master password is refused after the merge",
                  "Restored the backup, but its master password was not accepted." in log2, log2)
            check("restore: profile stays on the lock screen after failed unlock",
                  driver2.find_element(By.ID, "view-app").get_attribute("style").find("block") == -1,
                  driver2.find_element(By.ID, "view-app").get_attribute("style"))
            # the correct password still unlocks the untouched single-doc vault
            driver2.find_element(By.ID, "pw").send_keys("correct horse battery staple")
            driver2.find_element(By.ID, "btn-unlock").click()
            wait_for(driver2, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            t2 = [r.text for r in rows2]
            check("restore: wrong-password attempt left the vault intact and unlockable (manual 3.2/Q1)",
                  len(rows2) == 1 and KNOWN_TITLE in (t2[0] or ""), t2)

            # ---- a deleted doc cannot be resurrected by an OLD backup or tape
            # (用户手册 3.3: "删除会记录同步墓碑，之后即使从旧备份/旧同步磁带也
            # 无法 复活 该文档"). driver2's vault holds this exact workspace, so
            # the export_json bytes captured earlier ARE the pre-delete .kb of
            # the same doc (same salt, same id), and the tape captured below is
            # the pre-delete .kbsync. Delete the doc, then feed both back — the
            # tombstone must dominate (importKB skips tombstoned ids; applyTape
            # skips live entries dominated by a local tombstone).
            # capture the pre-delete tape first, while the doc is still live
            prekill_tape = driver2.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const { IndexedDBBackend } = await import('/idb.js');
                const { openKb } = await import('/src/factory.js');
                const { buildTape, serializeTape } = await import('/src/syncfile.js');
                const db = new IndexedDBBackend();
                const r = await openKb(db, 'correct horse battery staple');
                if (!r.ok) return cb('{"error":"unlock"}');
                const tape = await buildTape(r.params, db);
                cb(new TextDecoder().decode(serializeTape(tape)));
              })().catch((e) => cb('{"error":"' + String(e) + '"}'));
            """)
            driver2.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
            time.sleep(0.8)
            driver2.find_element(By.ID, "btn-delete").click()
            time.sleep(0.6)
            driver2.switch_to.alert.accept()  # confirm the deletion
            time.sleep(1.0)
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: delete confirmed on the restore profile",
                  len(rows2) == 0, [r.text for r in rows2])
            # in-app Import (场景二) of the OLD backup must respect the tombstone
            # that now lives in this vault: the doc's id is skipped on import,
            # so the list stays empty instead of the doc reappearing.
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'old.kb',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('import-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(kb_bytes))
            wait_for(driver2, EC.alert_is_present(), 8)
            driver2.switch_to.alert.accept()  # confirm: "Import 1 docs?..."
            time.sleep(2.0)
            log2 = driver2.find_element(By.ID, "log").text
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: old .kb import cannot revive the deleted doc (manual 3.3)",
                  len(rows2) == 0, (log2, [r.text for r in rows2]))
            # the old .kbsync tape (same workspace gate) must no-op against the
            # tombstone — a live entry at the deleted doc's timestamp is
            # dominated locally, so the doc stays gone
            driver2.execute_script("""
              const f = new File([new Uint8Array(arguments[0])], 'old.kbsync',
                                 { type: 'application/json' });
              const dt = new DataTransfer();
              dt.items.add(f);
              const inp = document.getElementById('sync-file');
              inp.files = dt.files;
              inp.dispatchEvent(new Event('change', { bubbles: true }));
            """, list(prekill_tape.encode("utf-8")))
            time.sleep(2.0)  # apply + openKb re-derive (PBKDF2) + refreshDocList
            log2 = driver2.find_element(By.ID, "log").text
            rows2 = driver2.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("resurrect: old .kbsync tape cannot revive the deleted doc (manual 3.3)",
                  len(rows2) == 0 and "skipped" in log2,
                  (log2, [r.text for r in rows2]))
        finally:
            driver2.quit()

        # ---- re-key: change master password without losing the vault
        # Save the dirty preview edits first so no unsaved-changes guard fires.
        driver.find_element(By.ID, "btn-save").click()
        time.sleep(1.2)
        NEW_PW = "brand new master password 7381"
        driver.find_element(By.ID, "rekey-current").send_keys("correct horse battery staple")
        driver.find_element(By.ID, "rekey-new").send_keys(NEW_PW)
        driver.find_element(By.ID, "rekey-confirm").send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-rekey").click()
        time.sleep(2.5)
        log = driver.find_element(By.ID, "log").text
        check("re-key: password changed (log confirms)",
              "Master password changed" in log and "re-wrapped" in log, log)

        # old password must now be refused at the unlock gate
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-unlock").click()
        time.sleep(1.2)
        log = driver.find_element(By.ID, "log").text
        check("re-key: old master password now denied", "Wrong master password" in log, log)

        # new password unlocks and the doc list still holds the known doc
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("re-key: new password unlocks vault with docs intact",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # The UI block above proves the re-key *receipt*, but manual 4.2 / FAQ
        # Q6 also promise the MECHANISM: "修改主密码后，全部 DEK 自动在新密钥下
        # 重新封装…旧密码即刻失效" — every stored per-document DEK wrapper must
        # be re-wrapped under the NEW workspace key and must no longer open with
        # the OLD key. That is invisible from the UI, so prove it by
        # cross-unwrapping the DEK blob before/after changeMasterPassword on a
        # SELF-CONTAINED scratch vault (fresh salt, one doc, its own IndexedDB
        # store 'local-kb-scratch-rekey'). The main vault is never touched and
        # stays under NEW_PW for the rest of the run.
        rekey_rot = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { createKb } = await import('/src/factory.js');
            const { changeMasterPassword } = await import('/src/rekey.js');
            const { unlockWorkspace } = await import('/src/workspace.js');
            const { deriveKeyBytes, deriveWorkspaceKey, importAesGcm, decrypt, utf8d } =
              await import('/src/crypto.js');
            const { deserializeDoc } = await import('/src/docmodel.js');
            const OLD_PW = 'rotation old pw 4711';
            const NEW_PW = 'rotation new pw 9420';
            const MARK = 'REKEY-ROTATION-PROOF-2d13';
            const scratch = new IndexedDBBackend('local-kb-scratch-rekey');
            const { store } = await createKb(scratch, OLD_PW, 'ops-vault');
            await store.create('doc-rk', 'Rotation Test',
                               [{ type: 'paragraph', text: 'before ' + MARK }]);
            const params = await scratch.getParams();
            const before = await scratch.getDoc('doc-rk');
            const oldDek = { iv: before.dekIv, data: before.dekData };
            // Soundness control: a key derived as the app derives it (same
            // salt/iterations/name) must open the OLD wrapper — else the later
            // "old key refuses the new wrapper" check would prove nothing.
            const rawOld = new Uint8Array(
              await deriveKeyBytes(OLD_PW, params.salt, params.iterations));
            const oldWsk = new Uint8Array(await deriveWorkspaceKey(rawOld, params.name));
            const oldH = await importAesGcm(oldWsk);
            let oldOpensOld = false;
            try { await decrypt(oldH, oldDek.iv, oldDek.data); oldOpensOld = true; } catch (e) {}
            const rk = await changeMasterPassword(scratch, OLD_PW, NEW_PW);
            const after = await scratch.getDoc('doc-rk');
            const rawNew = new Uint8Array(
              await deriveKeyBytes(NEW_PW, params.salt, params.iterations));
            const newWsk = new Uint8Array(await deriveWorkspaceKey(rawNew, params.name));
            const newH = await importAesGcm(newWsk);
            // (a) the stored (new) wrapper opens with the NEW key, (b) the OLD
            // key must now fail to unwrap it — "旧密码即刻失效" at the DEK level.
            let newDek = null;
            try { newDek = await decrypt(newH, after.dekIv, after.dekData); } catch (e) {}
            let oldFailsOnNew = false;
            try { await decrypt(oldH, after.dekIv, after.dekData); } catch (e) { oldFailsOnNew = true; }
            // (c) the doc content still decrypts and is unchanged after re-wrap
            let docGood = false;
            if (newDek) {
              const dekH = await importAesGcm(newDek);
              try {
                const d = deserializeDoc(utf8d.decode(await decrypt(dekH, after.iv, after.data)));
                docGood = d.title === 'Rotation Test' &&
                  (d.blocks || []).some((b) => (b.text || '').includes(MARK));
              } catch (e) {}
            }
            // (d) the new password authenticates, the old password is refused
            const newAuth = await unlockWorkspace(await scratch.getParams(), NEW_PW);
            const oldAuth = await unlockWorkspace(await scratch.getParams(), OLD_PW);
            return cb({
              oldOpensOld, rkOk: !!(rk && rk.ok), rkWrapped: (rk && rk.rewrapped) || 0,
              dekChanged: oldDek.iv !== after.dekIv || oldDek.data !== after.dekData,
              newOpensNew: !!newDek, oldFailsOnNew, docGood,
              newAuthOk: newAuth.ok === true, oldAuthDenied: oldAuth.ok === false,
            });
          })().catch((e) => cb({ error: String(e) }));
        """)
        rk2 = json.loads(json.dumps(rekey_rot))
        check("re-key: DEK re-wrapped under the new workspace key (manual 4.2)",
              rk2.get("oldOpensOld") is True and rk2.get("rkOk") is True and
              rk2.get("rkWrapped") >= 1 and rk2.get("dekChanged") is True,
              "wrapped=%s dek-changed=%s" % (rk2.get("rkWrapped"), rk2.get("dekChanged")))
        check("re-key: new key unwraps the DEK, old key cannot (manual 4.2/Q6)",
              rk2.get("newOpensNew") is True and rk2.get("oldFailsOnNew") is True,
              "new-opens=%s old-fails=%s" % (rk2.get("newOpensNew"), rk2.get("oldFailsOnNew")))
        check("re-key: doc content intact and unchanged after the rotation (manual 4.2/Q6)",
              rk2.get("docGood") is True, "doc-good=%s" % (rk2.get("docGood"),))
        check("re-key: new password authenticates, old password refused (manual 4.2/Q6)",
              rk2.get("newAuthOk") is True and rk2.get("oldAuthDenied") is True,
              "new-auth=%s old-denied=%s" % (rk2.get("newAuthOk"), rk2.get("oldAuthDenied")))

        # ---- FAQ Q1 + 3.1 安全提示: no recovery, and nothing recoverable is
        # stored (manual 5.3 Q1: "按设计无法找回。主密码为唯一解密凭据，系统不
        # 保存任何可恢复信息"; 3.1: "系统不会也不能提供找回功能"). Two
        # independent proofs: (a) the page exposes no forgot/reset/recover
        # affordance anywhere (the Restore-from-.kb card is NOT one — restoring
        # a backup still requires the master password itself), and (b) the
        # stored verifier decrypts to a fixed PUBLIC constant
        # (src/workspace.js VERIFIER_PLAINTEXT = 'local-kb::unlock-verifier::v1')
        # that is identical for every vault — never the password, so the stored
        # blob carries nothing that could be recovered into the password. The
        # storage proof runs on a SELF-CONTAINED scratch vault; the main vault
        # is untouched.
        recovery_scan = driver.execute_script("""
          const markers = ['forgot', 'reset', 'recover', '找回', '忘记', '重置'];
          const suspects = [];
          // every interactive control that could double as a recovery entry
          // point. The #log div is not interactive and is excluded, so log
          // tips like "…can never be recovered" cannot false-positive here.
          for (const el of document.querySelectorAll(
              'button, a, input, select, textarea, [role="button"], [onclick]')) {
            const hay = ((el.textContent || '') + ' ' + (el.id || '') + ' ' +
              (el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('title') || '') + ' ' +
              (el.getAttribute('aria-label') || '') + ' ' + (el.className || '')).toLowerCase();
            for (const m of markers) {
              if (hay.includes(m)) {
                suspects.push(m + '->' + (el.id || el.tagName) + ':' + hay.trim().slice(0, 80));
              }
            }
          }
          const idHits = Array.from(document.querySelectorAll('[id]'))
            .map((e) => e.id)
            .filter((id) => /forgot|reset|recover|找回|忘记|重置/i.test(id));
          return { suspects, idHits };
        """)
        rs = json.loads(json.dumps(recovery_scan))
        check("recovery: no forgot/reset/recover affordance anywhere in the UI (manual 3.1/Q1)",
              (not rs.get("suspects")) and (not rs.get("idHits")),
              "suspects=%r idHits=%r" % (rs.get("suspects"), rs.get("idHits")))

        q1_probe = driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { createKb } = await import('/src/factory.js');
            const { VERIFIER_PLAINTEXT, unlockWorkspace } = await import('/src/workspace.js');
            const { deriveKeyBytes, deriveWorkspaceKey, importAesGcm, decrypt, utf8d } =
              await import('/src/crypto.js');
            const Q1_PW = 'q1-password-probe-19x7';
            const scratch = new IndexedDBBackend('local-kb-scratch-q1');
            await createKb(scratch, Q1_PW, 'q1-probe-ws');
            const params = await scratch.getParams();
            const record = JSON.stringify(params);
            // re-derive the key exactly as unlockWorkspace would, then open the
            // verifier blob directly and read what it decrypts to
            const rawKey = new Uint8Array(
              await deriveKeyBytes(Q1_PW, params.salt, params.iterations));
            const wsKey = new Uint8Array(await deriveWorkspaceKey(rawKey, params.name));
            const handle = await importAesGcm(wsKey);
            let verifierText = null;
            try {
              verifierText = utf8d.decode(
                await decrypt(handle, params.verifier.iv, params.verifier.data));
            } catch (e) {}
            const wrong = await unlockWorkspace(params, 'not the password');
            const right = await unlockWorkspace(params, Q1_PW);
            return cb({
              decryptsToConstant: verifierText === VERIFIER_PLAINTEXT,
              plaintextIsConstant: VERIFIER_PLAINTEXT === 'local-kb::unlock-verifier::v1',
              notThePassword: verifierText !== Q1_PW,
              passwordAbsentFromRecord: !record.includes(Q1_PW),
              constantAbsentFromRecord: !record.includes(VERIFIER_PLAINTEXT),
              wrongPwRefused: wrong.ok === false,
              rightPwAccepted: right.ok === true,
            });
          })().catch((e) => cb({ error: String(e) }));
        """)
        q1 = json.loads(json.dumps(q1_probe))
        check("recovery: verifier decrypts to a fixed public constant, never the password (manual 5.3 Q1)",
              q1.get("decryptsToConstant") is True and q1.get("plaintextIsConstant") is True and
              q1.get("notThePassword") is True,
              "to-constant=%s not-password=%s" % (q1.get("decryptsToConstant"), q1.get("notThePassword")))
        check("recovery: stored workspace record holds no password or verifier plaintext (manual 5.3 Q1)",
              q1.get("passwordAbsentFromRecord") is True and q1.get("constantAbsentFromRecord") is True,
              "pw-in-record=%s const-in-record=%s" %
              (q1.get("passwordAbsentFromRecord") is False, q1.get("constantAbsentFromRecord") is False))
        check("recovery: verifier gate still refuses a wrong password (soundness, manual 3.1/Q1)",
              q1.get("wrongPwRefused") is True and q1.get("rightPwAccepted") is True,
              "wrong=%s right=%s" % (q1.get("wrongPwRefused"), q1.get("rightPwAccepted")))

        # ---- Lock: drop the master password from the session and clear memory
        driver.find_element(By.ID, "btn-lock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        check("lock: returns to the unlock view", True)
        pw_held = driver.execute_script("return sessionStorage.getItem('local-kb-pw');")
        check("lock: master password dropped from session",
              pw_held is None or pw_held == "", str(pw_held))
        editor = driver.find_element(By.ID, "editor-area").get_attribute("value")
        docrows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("lock: editor cleared and doc list emptied",
              editor == "" and len(docrows) == 0, f"editor={editor!r} rows={len(docrows)}")

        # re-unlock with the (new) master password; ciphertext is still on disk
        pw = driver.find_element(By.ID, "pw")
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        docs = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("lock: re-unlock shows the docs again (data intact)",
              any(KNOWN_TITLE in d.text for d in docs), [d.text for d in docs])

        # ---- idle auto-lock (manual 3.2: re-lock after a default idle period)
        # Shrink the idle window to real seconds via the documented page-global
        # override, then wait for the timer to fire on its own. Manual 3.2 also
        # promises "自动锁定前会自动保存未保存的编辑，不会丢失内容" — a dirty
        # editor edit must be auto-saved before the key is dropped, so open the
        # doc and type an UNSAVED edit first. The mousemove at the end re-arms
        # the timer after the last keydown; after that, no page interaction may
        # happen before the lock fires, or the test would just postpone it.
        driver.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
        time.sleep(1.0)
        area = driver.find_element(By.ID, "editor-area")
        marker = "\nIDLE-AUTOSAVE-PROOF"
        area.send_keys(marker)
        time.sleep(0.3)  # let the last input event mark the editor dirty
        driver.execute_script("""
          window.LOCAL_KB_IDLE_MS = 3000;
          document.dispatchEvent(new MouseEvent('mousemove'));
        """)
        time.sleep(4.2)
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        check("idle auto-lock: returns to the unlock gate", True)
        pw_held = driver.execute_script("return sessionStorage.getItem('local-kb-pw');")
        check("idle auto-lock: master password dropped from session",
              pw_held is None or pw_held == "", str(pw_held))
        log = driver.find_element(By.ID, "log").text
        check("idle auto-lock: log reports the inactivity lock",
              "auto-locked" in log.lower(), log)

        # re-unlock: the edit was never saved manually, so its survival proves
        # idleLock's auto-save ran before the session password was dropped
        pw = driver.find_element(By.ID, "pw")
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)
        driver.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
        time.sleep(1.0)
        area_val = driver.find_element(By.ID, "editor-area").get_attribute("value")
        check("idle auto-lock: unsaved edit auto-saved before the lock (manual 3.2)",
              "IDLE-AUTOSAVE-PROOF" in area_val, area_val[-120:])

        # The 3-second idle window was a test-only shrink for the block above.
        # Restore a long window and re-arm, or every later block would race the
        # auto-lock in its sleeps (the delete flow's modal confirm alone spans
        # several 3-second windows, and timers keep running while an alert is up).
        driver.execute_script("""
          window.LOCAL_KB_IDLE_MS = 3600000;
          document.dispatchEvent(new MouseEvent('mousemove'));
        """)

        # ---- FAQ Q7: unlocked plaintext lives only in page memory/tab (manual Q7)
        # "解锁状态下文档明文仅存在于本机内存与页签中，用于编辑与检索；关闭页面
        #  或锁定后即释放" — two co-observed halves: (a) while unlocked, the saved
        # doc plaintext (incl. the auto-saved IDLE-AUTOSAVE-PROOF canary) sits in
        # the editor DOM and the in-memory FTS index, yet appears in NONE of the
        # browser's persistence surfaces (IndexedDB, sessionStorage, localStorage);
        # (b) after the Lock button the in-page copy is released and a re-sweep of
        # the same surfaces stays clean — nothing on disk ever held recoverable
        # plaintext. The main vault is only read, never written, by this block.
        driver.find_element(By.CSS_SELECTOR, "#doc-list .doc-row").click()
        wait_for(driver, lambda d: "IDLE-AUTOSAVE-PROOF" in
                 d.find_element(By.ID, "editor-area").get_attribute("value"), 12)

        q7_js = """
        const cb = arguments[arguments.length - 1];
        (async () => {
          const editor = document.getElementById('editor-area').value;
          const title = document.getElementById('editor-title').value;
          const ls = {};
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i); ls[k] = localStorage.getItem(k);
          }
          const ss = {};
          for (let i = 0; i < sessionStorage.length; i++) {
            const k = sessionStorage.key(i); ss[k] = sessionStorage.getItem(k);
          }
          const req = indexedDB.open('local-kb');
          const db = await new Promise((res) => {
            req.onsuccess = () => res(req.result);
            req.onerror = () => res(null);
          });
          const recs = {};
          if (db) {
            for (const n of ['workspace', 'docs', 'meta']) {
              recs[n] = await new Promise((res) => {
                const q = db.transaction(n, 'readonly').objectStore(n).getAll();
                q.onsuccess = () => res(q.result);
                q.onerror = () => res([]);
              });
            }
            db.close();
          }
          cb({ editor, title, ls, ss, recs });
        })().catch((e) => cb({ error: String(e) }));
        """
        q7 = json.loads(json.dumps(driver.execute_async_script(q7_js), default=str))
        q7_needles = ["IDLE-AUTOSAVE-PROOF", "48.8584N", "oscar-7", "deflector",
                      KNOWN_BODY, "rendezvous coordinates", "Snowstorm"]
        q7_persist = json.dumps(
            {"ls": q7.get("ls"), "ss": q7.get("ss"), "recs": q7.get("recs")},
            default=str,
        )
        check("Q7: unlocked plaintext is present in page memory (editor shows the saved canary)",
              "IDLE-AUTOSAVE-PROOF" in q7.get("editor", ""), q7.get("editor", "")[-80:])
        q7_hits = [n for n in q7_needles if n in q7_persist]
        check("Q7: doc plaintext absent from IndexedDB + session/localStorage (manual FAQ Q7)",
              len(q7_hits) == 0, f"hits={q7_hits}")
        check("Q7: doc title also absent from every persistence surface (manual FAQ Q7)",
              KNOWN_TITLE not in q7_persist)

        # release half: the Lock button purges the in-page copies (editor/DOM
        # clearing is pinned above); the new proof is that a post-lock re-sweep
        # of all three persistence surfaces is still plaintext-free
        driver.find_element(By.ID, "btn-lock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
        q7l = json.loads(json.dumps(driver.execute_async_script(q7_js), default=str))
        q7l_editor = q7l.get("editor", "")
        q7l_persist = json.dumps(
            {"ls": q7l.get("ls"), "ss": q7l.get("ss"), "recs": q7l.get("recs")},
            default=str,
        )
        q7l_hits = [n for n in q7_needles if n in q7l_persist]
        check("Q7: after lock the RAM/DOM copy is released and storage is still clean (manual FAQ Q7)",
              q7l_editor == "" and len(q7l_hits) == 0,
              f"editor_len={len(q7l_editor)} hits={q7l_hits}")

        # re-unlock to restore the app state the delete block expects
        pw = driver.find_element(By.ID, "pw")
        pw.clear()
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list .doc-row")), 12)

        # IndexedDB must hold ciphertext only, with no new-password plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [NEW_PW, "correct horse battery staple"]:
            check(f"re-key: no master password plaintext in IndexedDB ('{needle}')", needle not in dump)

        # ---- document delete: confirm gate + tombstone + persistence
        # (用户手册 3.3: "删除文档：在文档列表对目标文档执行删除并确认；删除
        #  会记录同步墓碑" and 5.1 故障 5: "再检查是否误删（删除有二次确认）").
        # The delete flow must (a) do nothing until the confirm is accepted,
        # (b) leave a tombstone in the encrypted meta store, and (c) persist —
        # a reload + re-unlock must not resurrect the document. Pinned here at
        # the end because everything above (AC5 search, AC4 export, sync,
        # restore, re-key) needs this document to still exist.
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        target = next(r for r in rows if KNOWN_TITLE in (r.text or ""))
        target.click()
        time.sleep(1.0)
        driver.find_element(By.ID, "btn-delete").click()
        time.sleep(0.8)
        alert = driver.switch_to.alert
        check("delete: confirm dialog gates the deletion (manual 5.1 故障 5)", True)
        alert.dismiss()
        # the confirm returned false, so nothing may be deleted — but the doc
        # list re-renders around the dialog, so wait for it to settle instead
        # of racing a blind sleep against the recount (observed transiently
        # empty at this spot in cycle 46; the log below proves no deletion).
        wait_for(driver, EC.presence_of_element_located(
            (By.CSS_SELECTOR, "#doc-list .doc-row")), 8)
        time.sleep(0.3)
        log = driver.find_element(By.ID, "log").text
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: dismissing the confirm keeps the document",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        check("delete: dismissing the confirm logs no deletion", "Document deleted." not in log, log)

        # accept the confirm: the doc must leave the list, the editor clears,
        # and the log confirms the action (manual 3.3 "删除……并确认")
        wait_for(driver, EC.element_to_be_clickable((By.ID, "btn-delete")), 8)
        driver.find_element(By.ID, "btn-delete").click()
        time.sleep(0.8)
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(1.2)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: accepting the confirm removes the doc from the list",
              len(rows) == 0, [r.text for r in rows])
        log = driver.find_element(By.ID, "log").text
        check("delete: log confirms the deletion", "Document deleted." in log, log)
        title_val = driver.find_element(By.ID, "editor-title").get_attribute("value")
        check("delete: editor cleared after deletion", title_val == "", title_val)

        # a tombstone (the deleted doc's non-secret updatedAt) is kept in the
        # meta store so the delete propagates through the sync tape
        dump = flatten_dump(get_db_dump(driver))
        check("delete: tombstone recorded in IndexedDB meta (manual 3.3)",
              "tombstone:" in dump, dump[-200:])

        # reload + re-unlock: the delete must have persisted (AC1 proof), the
        # document stays gone instead of being resurrected from the store
        driver.refresh()
        wait_for(driver, EC.presence_of_element_located((By.ID, "view-unlock")), 12)
        pw = driver.find_element(By.ID, "pw")
        pw.send_keys(NEW_PW)
        driver.find_element(By.ID, "btn-unlock").click()
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        wait_for(driver, EC.presence_of_element_located((By.CSS_SELECTOR, "#doc-list")), 12)
        time.sleep(1.2)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("delete: deleted doc stays absent after reload + re-unlock",
              not any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])
        check("delete: doc list is empty after the only doc was deleted",
              len(rows) == 0, [r.text for r in rows])

        # ---- 用户手册 3.2: "解锁成功后自动重建全文索引" — the FTS index is
        # in-memory only and re-derived from the decrypted store on every
        # unlock, so it can never outlive the ciphertext it derives from. The
        # positive half (a fresh unlock makes current content searchable) is
        # pinned by every AC5 search above, each run on a just-unlocked
        # session. This block pins the discriminating *negative*: the doc
        # deleted above was searchable ("oscar" is one of its body plaintext
        # markers) in the previous session; after this reload + unlock the
        # search for that token must find nothing — a stale or persisted index
        # would still match it. A browser-level rebuild proof, not just a
        # unit-level one.
        search = driver.find_element(By.ID, "search-term")
        search.send_keys("oscar")
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.2)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("manual 3.2: rebuilt FTS index has no stale hit for a deleted doc",
              len(rows) == 0, [r.text for r in rows])
        list_body = driver.find_element(By.ID, "doc-list").text
        check("manual 3.2: empty search result rendered as no-match, not a stale list",
              "No matches." in list_body, list_body)
        # An empty-query Go restores the full list (refreshDocList), which now
        # renders the store's genuine empty state — proving the search plumbing
        # survived the probe and the negative above is data-true, not a broken
        # search silently returning nothing.
        search.clear()
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.2)
        list_body = driver.find_element(By.ID, "doc-list").text
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("manual 3.2: search plumbing intact after the rebuild probe",
              "No documents yet" in list_body and len(rows) == 0, list_body)

        # ---- AC6: offline — assert page JS/import loads (no network fallback)
        # The page itself is a static module graph served from /public; language
        # server already covered by reloads above. Also verify demo serves offline
        # static files with no external CDN references.
        html = driver.page_source
        check("AC6: page references no external network",
              all(x not in html for x in ["https://", "//cdn", "http://"]))

        # ---- 用户手册 4.1 (服务端口 4242，可用环境变量 PORT 覆盖), 5.1 故障 6
        # 处理 ("设置环境变量 PORT=其他端口后重启") and Q2 ("演示服务器仅提供
        # 静态文件传输服务"): pin the demo server's port + offline contract on
        # the wire — the /api/health offline marker, the default port serving
        # index.html, and a second instance started with PORT=<alt> answering on
        # that port. Stdlib http.client/subprocess only, no new dependencies.
        import http.client as _hc, socket as _sk, subprocess as _sp
        _host = BASE.split("://", 1)[1].rsplit(":", 1)[0]
        _port = int(BASE.split("://", 1)[1].rsplit(":", 1)[1])

        conn = _hc.HTTPConnection(_host, _port, timeout=5)
        conn.request("GET", "/api/health")
        hr = conn.getresponse()
        hbody = json.loads(hr.read().decode("utf-8"))
        conn.close()
        check("server: /api/health reports the offline static server (manual Q2)",
              hr.status == 200 and hbody.get("ok") is True
              and hbody.get("offline") is True and hbody.get("app") == "local-kb",
              f"status={hr.status} {hbody}")

        conn = _hc.HTTPConnection(_host, _port, timeout=5)
        conn.request("GET", "/")
        pr = conn.getresponse()
        pbody = pr.read().decode("utf-8", "replace")
        conn.close()
        check("server: default port serves the app's index.html (manual 2.1/4.1)",
              pr.status == 200 and "view-unlock" in pbody and "Local KB" in pbody,
              f"status={pr.status} bytes={len(pbody)}")

        # 用户手册 4.1 可配置项: "自动锁定 闲置 5 分钟" — the shipped default
        # must stay exactly 5 * 60 * 1000 ms. The idle-lock behavior above is
        # E2E-pinned through the window.LOCAL_KB_IDLE_MS override, so the real
        # default value itself is only attested here, at the served source.
        import urllib.request as _ur
        with _ur.urlopen(f"http://{_host}:{_port}/main.js", timeout=8) as _r:
            mjs = _r.read().decode("utf-8", "replace")
        _idle = re.search(r"IDLE_LOCK_MS\s*=\s*(\d+)\s*\*\s*60\s*\*\s*1000", mjs)
        check("server: served auto-lock default is 5 minutes (manual 4.1)",
              _idle is not None and int(_idle.group(1)) == 5,
              "IDLE_LOCK_MS" if _idle is None else _idle.group(0))

        # PORT override — a second demo instance bound to an ephemeral port must
        # answer on it (manual 4.1 "可用环境变量 PORT 覆盖" / 5.1 故障 6)
        _probe = _sk.socket()
        _probe.bind(("127.0.0.1", 0))
        _alt = _probe.getsockname()[1]
        _probe.close()
        _env = dict(os.environ)
        _env["PORT"] = str(_alt)
        _srv = _sp.Popen(
            ["node", "--enable-source-maps", "server/demo.mjs"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, env=_env,
        )
        try:
            _deadline = time.time() + 10
            _ok = False
            while time.time() < _deadline:
                try:
                    c2 = _hc.HTTPConnection("127.0.0.1", _alt, timeout=2)
                    c2.request("GET", "/api/health")
                    r2 = c2.getresponse()
                    h2 = json.loads(r2.read().decode("utf-8"))
                    c2.close()
                    if r2.status == 200 and h2.get("ok") is True:
                        _ok = True
                        break
                except Exception:
                    time.sleep(0.15)
            check("server: PORT env override rebinds the port (manual 4.1/故障 6)",
                  _ok, f"alt port {_alt}")
        finally:
            _srv.terminate()
            try:
                _srv.wait(timeout=5)
            finally:
                _srv.kill()

        # ---- 用户手册 2.1 "可将整个软件包根目录（含 public/ 与 src/…）部署到
        # 任意静态站点服务器（Nginx、Apache、Web 服务器等），效果一致":
        # the app must work *identically* when served by a plain static file
        # server with no demo routes at all. Everything above runs against
        # server/demo.mjs, which also answers /api/health; none of the page's
        # behavior may depend on that route. Prove it by serving the exact same
        # public/ + src/ tree from Python's stdlib http.server (bound to an
        # ephemeral port, emulating Nginx/Apache docroot resolution), then
        # driving the full create -> edit -> save -> list lifecycle over it —
        # MIME parity for the ES module graph included.
        import shutil as _sh, tempfile as _tf
        _tmpd = _tf.TemporaryDirectory()
        _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _publ = os.path.join(_repo, "public")
        _sroot = _tmpd.name
        for _fn in ("index.html", "main.js", "idb.js", "markdown.js"):
            _sh.copyfile(os.path.join(_publ, _fn), os.path.join(_sroot, _fn))
        os.symlink(os.path.join(_repo, "src"), os.path.join(_sroot, "src"))
        _probe2 = _sk.socket()
        _probe2.bind(("127.0.0.1", 0))
        _alt2 = _probe2.getsockname()[1]
        _probe2.close()
        _srv2 = _sp.Popen(
            [sys.executable, "-m", "http.server", str(_alt2),
             "--bind", "127.0.0.1", "--directory", _sroot],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        try:
            _deadline = time.time() + 10
            _up = False
            while time.time() < _deadline:
                try:
                    _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=2)
                    _c.request("GET", "/")
                    _r = _c.getresponse()
                    _b = _r.read().decode("utf-8", "replace")
                    _c.close()
                    if _r.status == 200 and "view-unlock" in _b:
                        _up = True
                        break
                except Exception:
                    time.sleep(0.15)
            check("static: a plain static file server serves the app (manual 2.1)",
                  _up, f"alt2 port {_alt2}")

            # the foreign server structurally cannot answer /api/health — the
            # app must need only static files, never demo.mjs's route
            _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=5)
            _c.request("GET", "/api/health")
            _r = _c.getresponse()
            _c.close()
            check("static: app needs no /api/health route (offline by construction)",
                  _r.status == 404, f"status={_r.status}")

            # module-graph MIME parity: .js must be served as a JS MIME type or
            # Firefox would refuse the ES module and the app would never boot
            _c = _hc.HTTPConnection("127.0.0.1", _alt2, timeout=5)
            _c.request("GET", "/main.js")
            _r = _c.getresponse()
            _mjs = _r.read().decode("utf-8", "replace")
            _ct = _r.getheader("Content-Type", "")
            _c.close()
            check("static: main.js served with a JS MIME + same bytes (module graph boots)",
                  _r.status == 200 and "javascript" in _ct and "IDLE_LOCK_MS" in _mjs,
                  _ct)

            # full lifecycle on the foreign origin: create vault -> new doc ->
            # save -> the doc lists (the same AC1 path, but with no demo server
            # behind the wire). Navigating resets the page, so wait for the
            # unlock gate to re-appear, then build a vault exactly like AC1.
            driver.get(f"http://127.0.0.1:{_alt2}/")
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            check("static: boots to the unlock gate on a foreign origin", True)
            driver.find_element(By.ID, "wsname").send_keys("static-parity")
            driver.find_element(By.ID, "pw2").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "pw2b").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "btn-create").click()
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            _lbl = driver.find_element(By.ID, "ws-label").text
            check("static: vault creates on the static origin (2.1 效果一致)",
                  "static-parity" in _lbl, _lbl)
            driver.find_element(By.ID, "btn-new").click()
            time.sleep(1.0)
            _t = driver.find_element(By.ID, "editor-title")
            _t.clear()
            _t.send_keys("Static Parity Doc")
            _a = driver.find_element(By.ID, "editor-area")
            _a.clear()
            _a.send_keys("served from a plain static file server; no demo routes.")
            driver.find_element(By.ID, "btn-save").click()
            time.sleep(1.5)
            _rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("static: create+edit+persist works end-to-end (manual 2.1)",
                  any("Static Parity Doc" in (r.text or "") for r in _rows),
                  [r.text for r in _rows])
        finally:
            _srv2.terminate()
            try:
                _srv2.wait(timeout=5)
            finally:
                _srv2.kill()

        # ---- 用户手册 2.2/Q4/Q8: "npm test 运行全部 88 项自动化测试" —
        # pin the headless-suite contract itself: run npm test from the repo
        # root and verify the TAP summary is exactly the number the manual
        # quotes (88 tests, 88 passing, 0 failing, exit 0). Self-contained
        # subprocess check, stdlib only.
        import os as _os, re as _re, subprocess as _sp
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _nt = "manual 2.2/Q4/Q8: npm test runs exactly the 88 documented automated tests"
        try:
            _r = _sp.run(["npm", "test"], cwd=_root,
                         capture_output=True, text=True, timeout=180)
            _out = (_r.stdout or "") + "\n" + (_r.stderr or "")
            _mt = _re.search(r"^# tests (\d+)$", _out, _re.M)
            _mp = _re.search(r"^# pass (\d+)$", _out, _re.M)
            _mf = _re.search(r"^# fail (\d+)$", _out, _re.M)
            _t = int(_mt.group(1)) if _mt else -1
            _p = int(_mp.group(1)) if _mp else -1
            _f = int(_mf.group(1)) if _mf else -1
            check(_nt, _r.returncode == 0 and _t == 88 and _p == 88 and _f == 0,
                  f"rc={_r.returncode} tests={_t} pass={_p} fail={_f}")
        except FileNotFoundError:
            check(_nt, False, "npm executable not found")
        except _sp.TimeoutExpired:
            check(_nt, False, "npm test timed out")

        # ---- 用户手册 1.3/2.2: "无需任何 npm 依赖" / "本软件无第三方运行时
        # 依赖" — pin the zero-runtime-dependency contract from the repo
        # itself: package.json must carry no runtime `dependencies` key (only
        # the fake-indexeddb dev/test helper), and every import in
        # src/ server/ public/ must resolve to a relative module or a
        # `node:` builtin. Self-contained, stdlib only.
        import json as _json, pathlib as _pl
        _nd = "manual 1.3/2.2: no runtime third-party dependencies (no deps key, only fake-indexeddb dev dep)"
        _rd = "manual 1.3/2.2: all imports in src/ server/ public/ are relative or node: builtins"
        _j = _json.loads(_pl.Path(_root, "package.json").read_text(encoding="utf-8"))
        _deps = dict(_j.get("dependencies") or {})
        _devs = dict(_j.get("devDependencies") or {})
        check(_nd, not _deps and sorted(_devs) == ["fake-indexeddb"],
              f"dependencies={sorted(_deps)} devDependencies={sorted(_devs)}")
        _bad = []
        for _sub in ("src", "server", "public"):
            for _f in sorted(_pl.Path(_root, _sub).rglob("*.js")) + \
                     sorted(_pl.Path(_root, _sub).rglob("*.mjs")):
                for _i, _ln in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
                    _m = _re.search(r'^\s*(?:import|export)\s+[^;\n]*?\s+from\s+["\']([^"\']+)["\']', _ln) \
                        or _re.search(r'^\s*require\(\s*["\']([^"\']+)["\']\s*\)', _ln)
                    if _m and not (_m.group(1).startswith(".") or _m.group(1).startswith("node:")):
                        _bad.append(f"{_f.name}:{_i}: {_m.group(1)}")
        check(_rd, not _bad, f"non-internal imports: {_bad[:5]}")

        # ---- 用户手册 2.2 / 4.3: "数据库文件按需在工作区首次创建时自动生成"
        # + "命令行模式：数据保存在工作目录下的 SQLite 数据库文件中" — pin the
        # CLI-mode storage contract against the repo implementation: the SQLite
        # vault file must appear only when the workspace is first created
        # (absent before), live under the scratch working directory, be a
        # genuine SQLite file, and a fresh process re-unlocking that same file
        # must still see a saved doc. Self-contained subprocess check.
        _cli_script = r'''
import { mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const repo = "file://__ROOT__/";
const { SQLiteBackend } = await import(repo + "src/db.js");
const { createKb, openKb } = await import(repo + "src/factory.js");
const dir = mkdtempSync(join(tmpdir(), "lke2e9-"));
const dbPath = join(dir, "vault.db");
const out = [];
out.push("pre:" + (existsSync(dbPath) ? "present" : "absent"));
let b = new SQLiteBackend(dbPath);
const kb = await createKb(b, "cycle9-pw", "default");
out.push("post:" + (existsSync(dbPath) ? "ok" : "missing"));
out.push("loc:" + (dbPath.startsWith(dir) ? "cwd" : "other"));
await kb.store.create("d1", "Title X", [{ type: "paragraph", text: "body y" }]);
b.close();
b = new SQLiteBackend(dbPath);
const u = await openKb(b, "cycle9-pw");
const rows = await b.listDocs();
b.close();
out.push("unlock:" + (u.ok ? "ok" : "fail"));
out.push("docs:" + (rows.length === 1 ? "one" : JSON.stringify(rows)));
const hdr = readFileSync(dbPath).subarray(0, 16).toString("latin1");
out.push("hdr:" + (hdr === "SQLite format 3" + String.fromCharCode(0) ? "sqlite" : "other"));
console.log(out.join(" "));
'''.replace("__ROOT__", _root)
        _cc = "manual 2.2/4.3: CLI mode auto-creates a working-directory SQLite file on first workspace creation; re-unlock sees saved docs"
        _r9 = _sp.run(["node", "--input-type=module", "-e", _cli_script],
                      capture_output=True, text=True, timeout=120)
        _o9 = (_r9.stdout or "").strip()
        check(_cc,
              _r9.returncode == 0 and _o9 == "pre:absent post:ok loc:cwd unlock:ok docs:one hdr:sqlite",
              f"rc={_r9.returncode} out={_o9!r} err={(_r9.stderr or '')[:300]!r}")

        # ---- 用户手册 5.1 故障 2: "解锁成功后文档列表为空" — the fix text
        # directs the user to confirm they unlocked the correct workspace and
        # notes that cleared browser site data needs a .kb backup restore.
        # Pin the app's side of that contract on an isolated fresh profile
        # (the "switched browser / cleared site data" premise): after a fresh
        # vault is unlocked, an empty doc list must render the explicit
        # empty-state guidance (renderDocList, main.js:68) rather than stale
        # rows or a blank pane, the workspace name must stay visible so the
        # right workspace is confirmable, and the .kb restore input must still
        # be on the lock screen for the manual's recovery path.
        _opts10 = Options()
        _opts10.headless = True
        _opts10.binary_location = fb
        _opts10.set_preference("dom.indexedDB.enabled", True)
        _d10 = webdriver.Firefox(options=_opts10)
        try:
            _d10.get(BASE + "/")
            wait_for(_d10, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            try:
                _d10.find_element(By.ID, "restore-file")
                _has_restore = True
            except Exception:
                _has_restore = False
            check("manual 5.1 故障 2: .kb restore input present on the lock screen (cleared-site-data recovery)",
                  _has_restore, "restore-file input")
            _d10.find_element(By.ID, "wsname").send_keys("cycle10-empty-ws")
            _d10.find_element(By.ID, "pw2").send_keys("cycle10 empty vault pw")
            _d10.find_element(By.ID, "pw2b").send_keys("cycle10 empty vault pw")
            _d10.find_element(By.ID, "btn-create").click()
            wait_for(_d10, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            # lock, then unlock in a fresh page load so the empty state below
            # is genuinely the post-unlock render, not the just-created DOM
            _d10.find_element(By.ID, "btn-lock").click()
            wait_for(_d10, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d10.find_element(By.ID, "pw").send_keys("cycle10 empty vault pw")
            _d10.find_element(By.ID, "btn-unlock").click()
            wait_for(_d10, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            time.sleep(1.2)
            _rows10 = _d10.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            _list10 = _d10.find_element(By.ID, "doc-list").text
            check("manual 5.1 故障 2: empty vault renders guidance, not stale rows or a blank list",
                  len(_rows10) == 0 and "No documents yet" in _list10, _list10)
            _label10 = _d10.find_element(By.ID, "ws-label").text
            check("manual 5.1 故障 2: workspace name confirms the correct workspace",
                  "cycle10-empty-ws" in _label10, _label10)
            check("manual 5.1 故障 2: user can create the first doc from the empty list",
                  _d10.find_element(By.ID, "btn-new").is_displayed(), True)
        finally:
            _d10.quit()

        # ---- 用户手册 2.4 卸载: browser mode "区别于删除部署目录，如需彻底清除
        # 本地数据…删除 IndexedDB 数据库（或以浏览器'清除站点数据'完成)"。
        # Pin the app side of that contract on an isolated fresh profile: dropping
        # the local database must behave as if the software was never installed —
        # the lock screen returns, the old master password no longer opens anything
        # (openKb 'no-workspace', main.js:427), the create-vault gate no longer
        # fires (main.js:447), and the old document's ciphertext is gone. We reload
        # after locking first so no live IndexedDB connection is open when the
        # delete runs (a held connection would make deleteDatabase block).
        _opts11 = Options()
        _opts11.headless = True
        _opts11.binary_location = fb
        _opts11.set_preference("dom.indexedDB.enabled", True)
        _d11 = webdriver.Firefox(options=_opts11)
        try:
            _d11.get(BASE + "/")
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d11.find_element(By.ID, "wsname").send_keys("cycle11-rm-ws")
            _d11.find_element(By.ID, "pw2").send_keys("cycle11 removal pw")
            _d11.find_element(By.ID, "pw2b").send_keys("cycle11 removal pw")
            _d11.find_element(By.ID, "btn-create").click()
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            _d11.find_element(By.ID, "btn-new").click()
            time.sleep(0.8)
            _d11.find_element(By.ID, "editor-title").clear()
            _d11.find_element(By.ID, "editor-title").send_keys("cycle11 old doc")
            _d11.find_element(By.ID, "editor-area").clear()
            _d11.find_element(By.ID, "editor-area").send_keys("plaintext that must not survive site-data removal")
            _d11.find_element(By.ID, "btn-save").click()
            time.sleep(1.2)
            _d11.find_element(By.ID, "btn-lock").click()
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            # Reload once so the app context (and its open IndexedDB connection)
            # is fully gone, then delete the database the way DevTools
            # "应用程序 → IndexedDB" / "清除站点数据" does.
            _d11.refresh()
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _del11 = _d11.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                "const req = indexedDB.deleteDatabase('local-kb');"
                "req.onsuccess = () => done('ok');"
                "req.onerror = () => done('err:' + (req.error && req.error.name));"
                "req.onblocked = () => done('blocked');")
            check("manual 2.4: IndexedDB database delete resolves (site-data removal)",
                  _del11 == "ok", _del11)
            _d11.refresh()
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d11.find_element(By.ID, "pw").send_keys("cycle11 removal pw")
            _d11.find_element(By.ID, "btn-unlock").click()
            time.sleep(1.2)
            _log11 = _d11.find_element(By.ID, "log").text
            check("manual 2.4: old master password is refused after removal (vault is gone)",
                  "Wrong master password" in _log11, _log11)
            _d11.find_element(By.ID, "wsname").send_keys("cycle11-fresh-ws")
            _d11.find_element(By.ID, "pw2").send_keys("cycle11 fresh pw")
            _d11.find_element(By.ID, "pw2b").send_keys("cycle11 fresh pw")
            _d11.find_element(By.ID, "btn-create").click()
            wait_for(_d11, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            _label11 = _d11.find_element(By.ID, "ws-label").text
            check("manual 2.4: a fresh vault can be created after removal (no vault-already-exists gate)",
                  "cycle11-fresh-ws" in _label11, _label11)
            _list11 = _d11.find_element(By.ID, "doc-list").text
            check("manual 2.4: removed database leaves no resurrected documents",
                  "cycle11 old doc" not in _list11 and "No documents yet" in _list11, _list11)
        finally:
            _d11.quit()

        # ---- 用户手册 4.2 (lines 214-215): "工作区密钥派生链：主密码─PBKDF2
        # (310,000)→解锁密钥─HKDF→工作区密钥；工作区密钥永不以任何形式写盘".
        # The mature 3.1/Q7 checks prove docs stay ciphertext and the key chain
        # participates, but none proves the workspace key BYTES themselves are
        # absent from every persistence surface. On an isolated fresh profile we
        # create a real vault via the UI (so sessionStorage/localStorage are
        # written exactly as the app writes them), derive the workspace key in
        # the page exactly as src/workspace.js does, prove the derivation is
        # genuinely the app's key by unwrapping a stored per-doc DEK with it
        # (positive control), and assert its base64 form appears nowhere in
        # IndexedDB, web storage, or the .kb export object.
        _opts12 = Options()
        _opts12.headless = True
        _opts12.binary_location = fb
        _opts12.set_preference("dom.indexedDB.enabled", True)
        _d12 = webdriver.Firefox(options=_opts12)
        try:
            _d12.get(BASE + "/")
            wait_for(_d12, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d12.find_element(By.ID, "wsname").send_keys("cycle12-ws")
            _d12.find_element(By.ID, "pw2").send_keys("cycle12 ws key pw")
            _d12.find_element(By.ID, "pw2b").send_keys("cycle12 ws key pw")
            _d12.find_element(By.ID, "btn-create").click()
            wait_for(_d12, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            wait_for(_d12, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            _d12.find_element(By.ID, "btn-new").click()
            time.sleep(0.8)
            _d12.find_element(By.ID, "editor-title").clear()
            _d12.find_element(By.ID, "editor-title").send_keys("cycle12 ws-key doc")
            _d12.find_element(By.ID, "editor-area").clear()
            _d12.find_element(By.ID, "editor-area").send_keys("body for the ws-key absence sweep")
            _d12.find_element(By.ID, "btn-save").click()
            time.sleep(1.2)
            _ws12 = _d12.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const { IndexedDBBackend } = await import('/idb.js');
                const { deriveKeyBytes, deriveWorkspaceKey, importAesGcm,
                        decrypt, toBase64 } = await import('/src/crypto.js');
                const { exportKB } = await import('/src/bak.js');
                const db = new IndexedDBBackend('local-kb');
                const params = await db.getParams();
                const raw = new Uint8Array(
                  await deriveKeyBytes('cycle12 ws key pw', params.salt, params.iterations));
                const wsk = new Uint8Array(await deriveWorkspaceKey(raw, params.name));
                // Positive control: the derived key must unwrap a stored per-doc
                // DEK, proving it IS the app's real workspace key — so the
                // absence checks below are meaningful, not a scan for a random
                // string.
                const ids = await db.listDocs();
                const docs = [];
                for (const r of ids) docs.push(await db.getDoc(r.id));
                const h = await importAesGcm(wsk);
                let sound = false;
                for (const d of docs) {
                  try { await decrypt(h, d.dekIv, d.dekData); sound = true; break; }
                  catch (e) {}
                }
                const exportBlob = JSON.stringify(exportKB(params, docs));
                return cb({
                  b64: toBase64(wsk),
                  sound,
                  iters: params.iterations,
                  wsName: params.name,
                  docs: docs.length,
                  exportBlob,
                });
              })().catch((e) => cb({ error: String(e) }));
            """)
            _wkr = json.loads(json.dumps(_ws12))
            _wkb64 = _wkr.get("b64", "")
            check("manual 4.2: derived workspace key is the app's real key " +
                  "(unwraps a stored DEK, positive control)",
                  _wkr.get("sound") is True and _wkr.get("docs", 0) >= 1 and
                  _wkr.get("iters") == 310000 and _wkr.get("wsName") == "cycle12-ws",
                  "sound=%s docs=%s iters=%s ws=%s" % (
                      _wkr.get("sound"), _wkr.get("docs"),
                      _wkr.get("iters"), _wkr.get("wsName")))
            _dump12 = flatten_dump(get_db_dump(_d12))
            check("manual 4.2: workspace key base64 absent from every IndexedDB record",
                  bool(_wkb64) and _wkb64 not in _dump12,
                  "b64len=%s dumpchars=%s" % (len(_wkb64), len(_dump12)))
            _web12 = _d12.execute_script("""
              const grab = (s) => {
                const out = [];
                for (let i = 0; i < s.length; i++) {
                  const k = s.key(i);
                  out.push(k + '=' + s.getItem(k));
                }
                return out.join('\\n');
              };
              return (sessionStorage.length ? grab(sessionStorage) + '\\n' : '') +
                     (localStorage.length ? grab(localStorage) : '');
            """)
            check("manual 4.2: workspace key base64 absent from session/local storage",
                  bool(_wkb64) and _wkb64 not in str(_web12),
                  "webstorage=%r" % (str(_web12)[:120],))
            check("manual 4.2: workspace key base64 absent from the exported .kb object",
                  bool(_wkb64) and _wkb64 not in str(_wkr.get("exportBlob", "")),
                  "exportchars=%s" % len(str(_wkr.get("exportBlob", ""))))
        finally:
            _d12.quit()

        # ---- 用户手册 3.1 (line 95): "若当前浏览器中已存在知识库，系统会拒绝
        # 新建并提示改用主密码解锁，以免覆盖既有工作区。"
        # Cycle 11 pinned the counterpart (create gate absent after vault removal);
        # this pins the gate ACTUALLY FIRING. main.js:447 refuses via
        # `if (vault) return log('A vault already exists in this browser — unlock
        # with its master password instead.', true)`. On an isolated fresh profile
        # we create a vault through the UI (so the create path is exercised),
        # lock, then attempt a SECOND create with a different name and master
        # password. The refusal must appear in #log, the original params (salt +
        # name) must be intact, only one workspace record may exist in
        # IndexedDB, and the original password must still unlock the vault.
        _opts13 = Options()
        _opts13.headless = True
        _opts13.binary_location = fb
        _opts13.set_preference("dom.indexedDB.enabled", True)
        _d13 = webdriver.Firefox(options=_opts13)
        try:
            _d13.get(BASE + "/")
            wait_for(_d13, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d13.find_element(By.ID, "wsname").send_keys("cycle13-first-ws")
            _d13.find_element(By.ID, "pw2").send_keys("cycle13 first pw")
            _d13.find_element(By.ID, "pw2b").send_keys("cycle13 first pw")
            _d13.find_element(By.ID, "btn-create").click()
            wait_for(_d13, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            wait_for(_d13, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            _d13.find_element(By.ID, "btn-lock").click()
            wait_for(_d13, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _params13 = _d13.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                "(async () => { const { IndexedDBBackend } = await import('/idb.js');"
                " const p = await new IndexedDBBackend('local-kb').getParams();"
                " done(p ? { name: p.name, salt: p.salt } : null); })().catch(() => done(null));")
            # Attempt a second workspace while the first vault is still present.
            _d13.find_element(By.ID, "wsname").send_keys("cycle13-second-ws")
            _d13.find_element(By.ID, "pw2").send_keys("cycle13 different pw")
            _d13.find_element(By.ID, "pw2b").send_keys("cycle13 different pw")
            _d13.find_element(By.ID, "btn-create").click()
            time.sleep(1.0)
            _log13 = _d13.find_element(By.ID, "log").text
            check("manual 3.1: creating a second vault is refused with an unlock prompt",
                  "A vault already exists in this browser" in _log13 and "unlock" in _log13,
                  _log13)
            check("manual 3.1: refusal leaves the app on the lock screen (no takeover)",
                  _d13.find_element(By.ID, "view-unlock").is_displayed(), True)
            _params13b = _d13.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                "(async () => { const { IndexedDBBackend } = await import('/idb.js');"
                " const p = await new IndexedDBBackend('local-kb').getParams();"
                " done(p ? { name: p.name, salt: p.salt } : null); })().catch(() => done(null));")
            check("manual 3.1: original vault params (name + salt) unchanged by the refusal",
                  _params13 == _params13b and bool(_params13b) and
                  _params13b.get("name") == "cycle13-first-ws",
                  "%r -> %r" % (_params13, _params13b))
            _dbdump13 = flatten_dump(get_db_dump(_d13))
            check("manual 3.1: exactly one workspace record exists (no second vault)",
                  _dbdump13.count("cycle13-first-ws") >= 1 and
                  "cycle13-second-ws" not in _dbdump13,
                  "first-ws=%d second-ws-absent=%s" % (
                      _dbdump13.count("cycle13-first-ws"),
                      "cycle13-second-ws" not in _dbdump13))
            # The original master password must still open the vault.
            _d13.find_element(By.ID, "pw").send_keys("cycle13 first pw")
            _d13.find_element(By.ID, "btn-unlock").click()
            wait_for(_d13, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            check("manual 3.1: original master password still unlocks the vault",
                  _d13.find_element(By.ID, "btn-lock").is_displayed(), True)
        finally:
            _d13.quit()

        # ---- 用户手册 4.1 (line 206): "全文索引 FTS5（内存，porter 分词）"
        # — pin the documented full-text-index parameters against the real
        # SQLite adapter (src/db.js:61 creates an in-memory FTS5 table with
        # tokenize='porter'). A doc whose body stores only the present-tense
        # "connecting" must be found by the past-tense query "connected"
        # (porter stems both to "connect"), the exact stored word must also
        # match, a title word "Networks" must answer to "network", an
        # unrelated term must stay silent (negative control), and the vault
        # file on disk must hold none of the plaintext corpus words (docs are
        # ciphertext and the index itself lives only in RAM, rebuilt from the
        # decrypted docs on every unlock — a fresh process unlocking the same
        # file must still answer search, proving the index is re-derived, not
        # persisted). Self-contained subprocess check, stdlib only, mirroring
        # the manual 2.2/4.3 CLI check above.
        _p_script = r'''
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const repo = "file://__ROOT__/";
const { SQLiteBackend } = await import(repo + "src/db.js");
const { createKb, openKb } = await import(repo + "src/factory.js");
const out = [];
const dir = mkdtempSync(join(tmpdir(), "lke2e14-"));
const dbPath = join(dir, "vault.db");
try {
  let b = new SQLiteBackend(dbPath);
  const kb = await createKb(b, "cycle14-pw", "default");
  await kb.store.create("d1", "Networks",
    [{ type: "paragraph", text: "Systems are connecting across sites" }]);
  await kb.store.create("d2", "News",
    [{ type: "paragraph", text: "The official announcement brings randomness to planning" }]);
  b.close();
  const raw = readFileSync(dbPath, "latin1");
  const onDisk = ["connecting", "Networks", "randomness"].every((w) => raw.includes(w));
  b = new SQLiteBackend(dbPath); // fresh process: index must be rebuilt on unlock
  const u = await openKb(b, "cycle14-pw");
  if (!u.ok) {
    out.push("unlock:fail");
  } else {
    const ids = async (q) => (await u.store.search(q)).map((h) => h.id).join(",");
    out.push("connect:" + ((await ids("connected")) === "d1" ? "ok" : await ids("connected")));
    out.push("reverse:" + ((await ids("connecting")) === "d1" ? "ok" : await ids("connecting")));
    out.push("title:" + ((await ids("network")) === "d1" ? "ok" : await ids("network")));
    out.push("neg:" + ((await ids("zzzebra")) === "" ? "ok" : await ids("zzzebra")));
    out.push("disk:" + (onDisk ? "leaked" : "ok"));
  }
  b.close();
} finally {
  rmSync(dir, { recursive: true, force: true });
}
console.log(out.join(" "));
'''.replace("__ROOT__", _root)
        _pname = "manual 4.1: FTS5 in-memory porter index (line 206)"
        _rp = _sp.run(["node", "--input-type=module", "-e", _p_script],
                      capture_output=True, text=True, timeout=120)
        _ptok = [t.split(":", 1) for t in ((_rp.stdout or "").strip().split()) if ":" in t]
        _pom = dict(_ptok)
        _pout = (_rp.stdout or "").strip() + "|err:" + ((_rp.stderr or "")[:200])
        check("manual 4.1: porter stems the search query ('connected' finds the doc storing 'connecting')",
              _rp.returncode == 0 and _pom.get("connect") == "ok", _pout)
        check("manual 4.1: porter stems the indexed term (exact stored word 'connecting' also matches)",
              _rp.returncode == 0 and _pom.get("reverse") == "ok", _pout)
        check("manual 4.1: porter stems titles (title 'Networks' matched by a search for 'network')",
              _rp.returncode == 0 and _pom.get("title") == "ok", _pout)
        check("manual 4.1: unrelated search terms yield no hits (negative control)",
              _rp.returncode == 0 and _pom.get("neg") == "ok", _pout)
        check("manual 4.1: plaintext corpus never hits the on-disk vault (index is in-memory)",
              _rp.returncode == 0 and _pom.get("disk") == "ok", _pout)

        # ---- 用户手册 2.3 (line 71) + 故障 6 (line 252): "浏览器：支持 WebCrypto
        # 的现代浏览器" / "确认浏览器支持 WebCrypto" — pin the browser runtime
        # requirement against the served page itself on a fresh headless profile:
        # the origin must expose WebCrypto (crypto.subtle), and the shipped
        # module graph (src/crypto.js, imported by public/main.js) must drive
        # genuine AES-256-GCM rounds through it — a non-extractable secret key
        # with the documented usages, a byte-exact encrypt(decrypt()) round-trip,
        # and a fresh 12-byte IV per message. An in-process negative control then
        # strips globalThis.crypto and shows the same module refuses to derive
        # (TypeError reading 'subtle'): no degraded fallback, which is exactly
        # what 故障 6 tells the user to confirm.
        _opts15 = Options()
        _opts15.headless = True
        _opts15.binary_location = fb
        _opts15.set_preference("dom.indexedDB.enabled", True)
        _d15 = webdriver.Firefox(options=_opts15)
        try:
            _d15.get(BASE + "/")
            wait_for(_d15, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _w15 = _d15.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const r = { subtle: !!globalThis.crypto && !!globalThis.crypto.subtle };
                try {
                  const { deriveKeyBytes, deriveWorkspaceKey, importAesGcm,
                          encrypt, decrypt, utf8e, utf8d } = await import('/src/crypto.js');
                  const raw = new Uint8Array(await deriveKeyBytes("pin-pw",
                      "c2FsdHNhbHRzYWx0c2FsdA", 3100));
                  const ws = new Uint8Array(await deriveWorkspaceKey(raw, "ws15"));
                  const key = await importAesGcm(ws);
                  r.keyType = key.type;
                  r.keyAlg = ((key.algorithm || {}).name || "") + "/" +
                             ((key.algorithm || {}).length || 0);
                  r.extractable = key.extractable;
                  r.usages = key.usages.join(",");
                  const msg = utf8e.encode("Local KB WebCrypto round-trip");
                  const first = await encrypt(key, msg);
                  const second = await encrypt(key, msg);
                  const back = new Uint8Array(await decrypt(key, first.iv, first.data));
                  r.ok = utf8d.decode(back) === "Local KB WebCrypto round-trip";
                  r.ivBytes = Math.floor(first.iv.length * 3 / 4);
                  r.freshIv = first.iv !== second.iv;
                } catch (e) {
                  r.error = e.name + ": " + String(e.message).slice(0, 120);
                }
                cb(r);
              })().catch((e) => cb({ error: String(e) }));
            """)
            check("manual 2.3/故障 6: served origin exposes WebCrypto (crypto.subtle present)",
                  bool(_w15.get("subtle")), _w15)
            check("manual 2.3: shipped crypto drives a genuine AES-256-GCM key (secret, non-extractable)",
                  _w15.get("keyType") == "secret" and "AES-GCM" in _w15.get("keyAlg", "")
                  and _w15.get("extractable") is False, _w15)
            check("manual 2.3: derived key is good for encrypt/decrypt/wrapKey/unwrapKey",
                  _w15.get("usages") == "encrypt,decrypt,wrapKey,unwrapKey", _w15.get("usages"))
            check("manual 2.3: AES-GCM round-trip through the served module is byte-exact",
                  _w15.get("ok") is True, _w15)
            check("manual 2.3: each encrypt() issues a fresh 12-byte IV (AES-GCM)",
                  _w15.get("ivBytes") == 12 and _w15.get("freshIv") is True, _w15)
        finally:
            _d15.quit()

        # Negative control without WebCrypto (Node): strip globalThis.crypto from
        # the same module graph — no crutch polyfill must keep it alive.
        _w_script = r'''
        const { deriveUnlockKey } = await import("file://__ROOT__/src/crypto.js");
        Object.defineProperty(globalThis, "crypto",
          { value: undefined, configurable: true, writable: true });
        let threw = "";
        try { await deriveUnlockKey("pw", "c2FsdHNhbHRzYWx0c2FsdA"); }
        catch (e) { threw = e.name + ":" + String(e.message).slice(0, 120); }
        console.log("refuse:" + (/subtle/.test(threw) ? "ok" : threw));
        '''.replace("__ROOT__", _root)
        _wr = _sp.run(["node", "--input-type=module", "-e", _w_script],
                      capture_output=True, text=True, timeout=60)
        _wout = (_wr.stdout or "").strip() + "|err:" + ((_wr.stderr or "")[:160])
        check("manual 2.3/故障 6: without WebCrypto the app crypto refuses (no fallback)",
              _wr.returncode == 0 and "refuse:ok" in _wout, _wout)

        # ---- 用户手册 2.1 (lines 55-56) + Q2 (line 272): "服务端仅负责文件
        # 传输，不存储任何业务数据" / "演示服务器仅提供静态文件传输服务，不保存
        # 业务数据" — pin the shipped demo server's statelessness on the wire:
        # (a) static contract: demo.mjs may only read from disk — the source
        # must contain no write/mkdir/rename/unlink/append call at all; and
        # (b) runtime contract: a full real user session — create vault, save a
        # doc carrying a unique canary — run against a live demo.mjs instance
        # (PORT-alt, ephemeral socket, same spawn idiom as the 4.1/故障 6 block)
        # must leave the served tree byte-for-byte unchanged, with the canary
        # appearing in no served file: the only durable state is the browser's
        # IndexedDB, never server-side storage.
        import hashlib as _hlib16
        import http.client as _hc16, socket as _sk16, subprocess as _sp16
        _repo16 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _skip16 = {".git", ".venv", "node_modules", "tests", ".future", "vendor"}

        def _served_tree16():
            _tr = {}
            for _dp, _dns, _fns in os.walk(_repo16):
                _dns[:] = sorted(d for d in _dns if d not in _skip16)
                for _fn in sorted(_fns):
                    _abs = os.path.join(_dp, _fn)
                    with open(_abs, "rb") as _fh:
                        _tr[os.path.relpath(_abs, _repo16)] = \
                            _hlib16.sha256(_fh.read()).hexdigest()
            return _tr

        _wops16 = []
        for _i, _ln in enumerate(open(os.path.join(_repo16, "server", "demo.mjs"),
                                      encoding="utf-8").read().splitlines(), 1):
            _s = _ln.strip()
            if re.search(r"\b(writeFileSync|writeFile|createWriteStream|appendFile"
                         r"|appendFileSync|writeSync|write\(|mkdir|rmdir|rmSync"
                         r"|renameSync|unlink|truncate|copyFile)\s*\(", _s):
                _wops16.append(f"{_i}: {_s}")
        check("manual 2.1/Q2: demo server source performs no filesystem writes (read-only static server)",
              not _wops16, str(_wops16[:5]) or "no write calls in demo.mjs")
        _canary16 = "srv-stateless-%x" % int(time.time() * 1e9)
        _before16 = _served_tree16()
        _probe16 = _sk16.socket()
        _probe16.bind(("127.0.0.1", 0))
        _alt16 = _probe16.getsockname()[1]
        _probe16.close()
        _env16 = dict(os.environ)
        _env16["PORT"] = str(_alt16)
        _srv16 = _sp16.Popen(
            ["node", "--enable-source-maps", "server/demo.mjs"],
            cwd=_repo16, stdout=_sp16.DEVNULL, stderr=_sp16.DEVNULL, env=_env16,
        )
        try:
            _deadline = time.time() + 10
            _up16 = False
            while time.time() < _deadline:
                try:
                    _cb = _hc16.HTTPConnection("127.0.0.1", _alt16, timeout=2)
                    _cb.request("GET", "/api/health")
                    _rb = _cb.getresponse()
                    _hb = json.loads(_rb.read().decode("utf-8"))
                    _cb.close()
                    if _rb.status == 200 and _hb.get("ok") is True:
                        _up16 = True
                        break
                except Exception:
                    time.sleep(0.15)
            check("manual 2.1/Q2: demo server instance answers on its ephemeral port",
                  _up16, f"alt port {_alt16}")

            driver.get(f"http://127.0.0.1:{_alt16}/")
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            driver.find_element(By.ID, "wsname").send_keys("stateless-probe")
            driver.find_element(By.ID, "pw2").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "pw2b").send_keys("correct horse battery staple")
            driver.find_element(By.ID, "btn-create").click()
            wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            _lbl16 = driver.find_element(By.ID, "ws-label").text
            check("manual 2.1/Q2: vault creates under the demo server origin (2.1 效果一致)",
                  "stateless-probe" in _lbl16, _lbl16)
            driver.find_element(By.ID, "btn-new").click()
            time.sleep(1.0)
            _t16 = driver.find_element(By.ID, "editor-title")
            _t16.clear()
            _t16.send_keys("Stateless Session Doc")
            _a16 = driver.find_element(By.ID, "editor-area")
            _a16.clear()
            _a16.send_keys("demo-server session body " + _canary16)
            driver.find_element(By.ID, "btn-save").click()
            time.sleep(1.5)
            _rows16 = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("manual 2.1/Q2: full user session completes on the demo server (vault + saved doc)",
                  any("Stateless Session Doc" in (r.text or "") for r in _rows16),
                  [r.text for r in _rows16])

            _leak16 = []
            for _rel16, _hash16 in _served_tree16().items():
                _ab = os.path.join(_repo16, _rel16)
                if os.path.getsize(_ab) < 2 * 1024 * 1024:
                    with open(_ab, "r", encoding="utf-8", errors="replace") as _fh:
                        if _canary16 in _fh.read():
                            _leak16.append(_rel16)
            check("manual 2.1/Q2: doc body canary appears in no served file (no server-side storage)",
                  not _leak16, str(_leak16[:5]) or "canary absent from every served file")

            _after16 = _served_tree16()
            _diff16 = {k: (_before16[k], _after16.get(k))
                       for k in set(_before16) | set(_after16)
                       if _before16.get(k) != _after16.get(k)}
            check("manual 2.1/Q2: served tree byte-identical after the session (server stores no business data)",
                  not _diff16, str(_diff16)[:400] or "all served files unchanged")
        finally:
            _srv16.terminate()
            try:
                _srv16.wait(timeout=5)
            finally:
                _srv16.kill()

        # ---- 用户手册 4.2 (lines 214-215): "每个文档使用独立随机 DEK；DEK 由
        # 工作区密钥 AES-256-GCM 封装后与文档密文一同保存". Cycle 12 proved the
        # key chain and single-doc wrapped-DEK soundness; but NO existing check
        # compares DEK material across two documents. Here we create a real vault
        # on an isolated fresh profile, save TWO distinct docs through the UI,
        # then derive the workspace key in-page (same idiom as Cycle 12),
        # unwrap BOTH stored per-doc DEK wrappers, and assert: (a) both unwrap
        # cleanly (soundness), (b) the two raw DEK byte strings DIFFER
        # (independence — a fresh randomBytes(32) per create(), src/store.js),
        # (c) each raw DEK decrypts its own doc's ciphertext to its own plaintext
        # and FAILS to decrypt the other doc's ciphertext (each DEK governs only
        # its own document).
        _opts17 = Options()
        _opts17.headless = True
        _opts17.binary_location = fb
        _opts17.set_preference("dom.indexedDB.enabled", True)
        _d17 = webdriver.Firefox(options=_opts17)
        try:
            _d17.get(BASE + "/")
            wait_for(_d17, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d17.find_element(By.ID, "wsname").send_keys("cycle17-dek-ws")
            _d17.find_element(By.ID, "pw2").send_keys("cycle17 per-doc dek pw")
            _d17.find_element(By.ID, "pw2b").send_keys("cycle17 per-doc dek pw")
            _d17.find_element(By.ID, "btn-create").click()
            wait_for(_d17, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(_d17, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            _lbl17 = _d17.find_element(By.ID, "ws-label").text
            check("manual 4.2: vault created for per-doc DEK independence probe",
                  "cycle17-dek-ws" in _lbl17, _lbl17)

            def _save_doc17(title, body):
                _d17.find_element(By.ID, "btn-new").click()
                time.sleep(0.8)
                _t17 = _d17.find_element(By.ID, "editor-title")
                _t17.clear()
                _t17.send_keys(title)
                _a17 = _d17.find_element(By.ID, "editor-area")
                _a17.clear()
                _a17.send_keys(body)
                _d17.find_element(By.ID, "btn-save").click()
                time.sleep(1.2)

            _save_doc17("Doc Alpha",
                        "alpha body text for the per-doc DEK independence check")
            _save_doc17("Doc Beta",
                        "beta body — must be encrypted under its own fresh DEK")
            _rows17 = _d17.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            _rowtxt17 = [r.text for r in _rows17]
            check("manual 4.2: both probe documents saved through the UI",
                  any("Doc Alpha" in t for t in _rowtxt17) and
                  any("Doc Beta" in t for t in _rowtxt17), _rowtxt17)

            _dek17 = _d17.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const { IndexedDBBackend } = await import('/idb.js');
                const { deriveKeyBytes, deriveWorkspaceKey, importAesGcm,
                        decrypt, toBase64, fromBase64 } = await import('/src/crypto.js');
                const db = new IndexedDBBackend('local-kb');
                const params = await db.getParams();
                const raw = new Uint8Array(
                  await deriveKeyBytes('cycle17 per-doc dek pw', params.salt,
                                       params.iterations));
                const wsk = new Uint8Array(await deriveWorkspaceKey(raw, params.name));
                const h = await importAesGcm(wsk);
                const ids = await db.listDocs();
                const recs = {};
                for (const r of ids) {
                  const d = await db.getDoc(r.id);
                  const dekBytes = await decrypt(h, d.dekIv, d.dekData);
                  const ptOwn = await decrypt(await importAesGcm(dekBytes),
                                              d.iv, d.data);
                  recs[d.id] = { dek: toBase64(dekBytes), iv: d.iv, data: d.data,
                                 plain: new TextDecoder().decode(ptOwn) };
                }
                // Doc IDs are random, so sorted order is NOT creation order.
                // Identify each probe document by its own decrypted plaintext
                // (content-addressed) so the per-doc DEK mapping is robust.
                const all = Object.keys(recs).sort();
                let idA = null, idB = null;
                for (const k of all) {
                  if (recs[k].plain.includes('Doc Alpha')) idA = k;
                  else if (recs[k].plain.includes('Doc Beta')) idB = k;
                }
                // Cross-decrypt: doc B's ciphertext must FAIL under doc A's DEK
                // (GCM auth tag rejects the wrong key) — proving each DEK governs
                // only its own document.
                let cross = null;
                try {
                  const hA = await importAesGcm(fromBase64(recs[idA].dek));
                  await decrypt(hA, recs[idB].iv, recs[idB].data);
                  cross = 'wrong-key-decrypted';
                } catch (e) { cross = 'rejected'; }
                return cb({ idA, idB, dekA: recs[idA].dek, dekB: recs[idB].dek,
                            plainA: recs[idA].plain, plainB: recs[idB].plain,
                            cross });
              })().catch((e) => cb({ error: String(e) }));
            """)
            _dk17 = json.loads(json.dumps(_dek17))
            check("manual 4.2: both stored per-doc DEKs unwrap cleanly with the " +
                  "workspace key (soundness)",
                  not _dk17.get("error") and _dk17.get("dekA") and _dk17.get("dekB"),
                  str(_dk17)[:200])
            check("manual 4.2: the two documents carry DIFFERENT random DEK bytes",
                  bool(_dk17.get("dekA")) and _dk17.get("dekA") != _dk17.get("dekB"),
                  "A=%s... B=%s..." % (str(_dk17.get("dekA"))[:24],
                                       str(_dk17.get("dekB"))[:24]))
            check("manual 4.2: each DEK decrypts its own document's ciphertext to " +
                  "its own plaintext",
                  bool(_dk17.get("plainA")) and bool(_dk17.get("plainB")) and
                  "Doc Alpha" in _dk17.get("plainA", "") and
                  "alpha body" in _dk17.get("plainA", "") and
                  "Doc Beta" in _dk17.get("plainB", "") and
                  "beta body" in _dk17.get("plainB", ""),
                  "plainA=%.60r plainB=%.60r" % (_dk17.get("plainA"),
                                                 _dk17.get("plainB")))
            check("manual 4.2: a doc's ciphertext rejects decryption under the " +
                  "other document's DEK (GCM tag mismatch)",
                  _dk17.get("cross") == "rejected", str(_dk17.get("cross")) or "no error")
        finally:
            _d17.quit()

        # ---- 用户手册 5.3 Q3: "软件需要联网才能使用吗？A：不需要。创建、
        # 编辑、检索、备份、修改主密码全部离线可用；同步采用离线文件
        # (.kbsync) 或自建渠道传递。" The AC6 check above only greps the served
        # HTML for URL strings; nothing yet pins the RUNTIME behavior — that the
        # five documented capabilities complete without a single fetch/XHR/
        # sendBeacon call and that every resource the page ever loaded comes from
        # the app's own origin (never a CDN/telemetry host). This block arms
        # in-page observers on a fresh profile, walks all five capabilities in
        # order, then audits (a) the live API-call trace and (b) the page's full
        # performance resource list. IndexedDB/WebCrypto local work counts as
        # offline; any real HTTP(S) API call would fail the trace.
        _opts18 = Options()
        _opts18.headless = True
        _opts18.binary_location = fb
        _opts18.set_preference("dom.indexedDB.enabled", True)
        _d18 = webdriver.Firefox(options=_opts18)
        try:
            _d18.get(BASE + "/")
            wait_for(_d18, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d18.execute_script("""
              // Arm the wire observers BEFORE the first capability action. Every
              // fetch/XMLHttpRequest/sendBeacon from now on is recorded verbatim;
              // original behavior is preserved so the app is unaffected.
              window.__wireTrace = [];
              const rec = (t, u) => window.__wireTrace.push({ t: t, u: String(u).slice(0, 500) });
              const _fetch = window.fetch;
              window.fetch = function (...a) {
                rec('fetch', a[0] && a[0].url ? a[0].url : a[0]);
                return _fetch.apply(this, a);
              };
              const _open = XMLHttpRequest.prototype.open;
              const _send = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.open = function (m, u, ...r) {
                this.__u = u; return _open.call(this, m, u, ...r);
              };
              XMLHttpRequest.prototype.send = function (...a) {
                if (this.__u) rec('xhr', this.__u);
                return _send.apply(this, a);
              };
              if (navigator.sendBeacon) {
                const _beacon = navigator.sendBeacon.bind(navigator);
                navigator.sendBeacon = function (u, ...a) {
                  rec('beacon', u); return _beacon(u, ...a);
                };
              }
              return true;
            """)
            check("manual 5.3/Q3: wire observer armed on the fresh page (non-vacuous)",
                  _d18.execute_script("return Array.isArray(window.__wireTrace)"),
                  "observer list missing")

            # 1) 创建 — fresh vault entirely through the UI (like Cycle 17).
            _d18.find_element(By.ID, "wsname").send_keys("cycle18-offline-ws")
            _d18.find_element(By.ID, "pw2").send_keys("cycle18 offline pw")
            _d18.find_element(By.ID, "pw2b").send_keys("cycle18 offline pw")
            _d18.find_element(By.ID, "btn-create").click()
            wait_for(_d18, EC.visibility_of_element_located((By.ID, "view-app")), 15)
            wait_for(_d18, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            _lbl18 = _d18.find_element(By.ID, "ws-label").text
            check("manual 5.3/Q3: create capability succeeds offline (workspace unlocked)",
                  "cycle18-offline-ws" in _lbl18, _lbl18)

            # 2) 编辑 — save a new document through the UI.
            _d18.find_element(By.ID, "btn-new").click()
            time.sleep(0.8)
            _t18 = _d18.find_element(By.ID, "editor-title")
            _t18.clear()
            _t18.send_keys("Offline Ops")
            _a18 = _d18.find_element(By.ID, "editor-area")
            _a18.clear()
            _a18.send_keys("offline walk token kauri 71fj9")
            _d18.find_element(By.ID, "btn-save").click()
            time.sleep(1.2)
            _rows18 = _d18.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("manual 5.3/Q3: edit capability succeeds offline (doc saved + listed)",
                  any("Offline Ops" in (r.text or "") for r in _rows18),
                  [r.text for r in _rows18])

            # 3) 检索 — the in-memory FTS index answers a body-token query.
            _s18 = _d18.find_element(By.ID, "search-term")
            _s18.send_keys("kauri")
            _d18.find_element(By.ID, "btn-search").click()
            time.sleep(1.2)
            _rows18 = _d18.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
            check("manual 5.3/Q3: search capability succeeds offline (body-token hit)",
                  any("Offline Ops" in (r.text or "") for r in _rows18),
                  [r.text for r in _rows18])
            _s18.clear()
            _d18.find_element(By.ID, "btn-search").click()
            time.sleep(1.0)

            # 4) 备份 — the real Export .kb button produces the stamped name
            # (anchor-click capture idiom, no browser download to disk).
            _d18.execute_script("""
              window.__offlineExport = null;
              const _o = HTMLAnchorElement.prototype.click;
              HTMLAnchorElement.prototype.click = function () {
                if (this.download) window.__offlineExport = this.download;
                _o.call(this);
              };
              document.getElementById('btn-export').click();
              return true;
            """)
            time.sleep(0.5)
            _exp18 = _d18.execute_script("return window.__offlineExport")
            check("manual 5.3/Q3: backup capability succeeds offline (.kb stamped name)",
                  bool(re.match(r"^local-kb-backup-\d{4}-\d{2}-\d{2}-.*\.kb$",
                                _exp18 or "")), _exp18)

            # 5) 修改主密码 — re-key through the UI with the current password.
            _d18.find_element(By.ID, "rekey-current").send_keys("cycle18 offline pw")
            _d18.find_element(By.ID, "rekey-new").send_keys("cycle18 offline pw v2")
            _d18.find_element(By.ID, "rekey-confirm").send_keys("cycle18 offline pw v2")
            _d18.find_element(By.ID, "btn-rekey").click()
            time.sleep(2.5)
            _log18 = _d18.find_element(By.ID, "log").text
            check("manual 5.3/Q3: rekey capability succeeds offline (log receipt)",
                  "Master password changed" in _log18 and "re-wrapped" in _log18,
                  _log18)

            # Audit (a): the live wire trace during the five-capability walk.
            _trace18 = _d18.execute_script("return window.__wireTrace")
            _api18 = [x for x in _trace18 if _trace18 and str(x.get("t", ""))
                      and not str(x.get("u", "")).lower().startswith(
                          ("blob:", "data:", "about:"))]
            check("manual 5.3/Q3: not one fetch/XHR/beacon call during the walk",
                  isinstance(_trace18, list) and len(_api18) == 0, str(_api18)[:300])

            # Audit (b): every subresource the page ever requested across its
            # whole lifetime is same-origin — the module graph, icon data, etc.
            # A CDN/telemetry host would appear here as a foreign origin.
            _res18 = _d18.execute_script("""
              return performance.getEntriesByType('resource')
                       .map((e) => e.name);
            """)
            _org18 = (BASE.split("://", 1)[1].rsplit(":", 1)[0])
            _foreign18 = [n for n in (_res18 or [])
                          if n.startswith(("http://", "https://"))
                          and not n.split("://", 1)[1].startswith(_org18)]
            check("manual 5.3/Q3: every page-lifetime resource is app-origin " +
                  "(no external CDN/telemetry)",
                  isinstance(_res18, list) and len(_foreign18) == 0,
                  str(_foreign18)[:300])
        finally:
            _d18.quit()

        # ---- 用户手册 4.1 可配置项一览: "备份格式 .kb（JSON，仅密文）" and
        # "同步格式 .kbsync（文本，仅密文）". AC4 and M3 already pin that the
        # exported bytes carry the crypto parameters and that no *plaintext
        # needles* appear; nothing yet pins the FORMAT CONTRACT itself — that
        # both files are valid JSON with the documented top-level schema (app /
        # format / timestamp / workspace / documents|entries), that every
        # payload field (dekIv, dekData, iv, data; workspace salt + verifier)
        # is a decodable URL-safe-base64 ciphertext blob, and thus that
        # "仅密文" holds at the field level, not just as a needle search. This
        # block builds a fresh workspace through the same modules the Export /
        # Sync out buttons call (bak.js / syncfile.js) and audits the two files.
        _opts19 = Options()
        _opts19.headless = True
        _opts19.binary_location = fb
        _opts19.set_preference("dom.indexedDB.enabled", True)
        _d19 = webdriver.Firefox(options=_opts19)
        try:
            _d19.get(BASE + "/")
            wait_for(_d19, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _fmt19 = json.loads(json.dumps(_d19.execute_async_script("""
              const cb = arguments[arguments.length - 1];
              (async () => {
                const { IndexedDBBackend } = await import('/idb.js');
                const { createKb } = await import('/src/factory.js');
                const { exportKB, serializeKB } = await import('/src/bak.js');
                const { buildTape, serializeTape } = await import('/src/syncfile.js');
                const { uuid } = await import('/src/util.js');
                const { fromBase64 } = await import('/src/crypto.js');
                const db = new IndexedDBBackend('local-kb-c19');
                const r = await createKb(db, 'cycle19 fmt pw', 'cycle19-format-ws');
                if (!r || !r.store) return cb({ error: 'createKb failed' });
                const mk = (t, b) => r.store.create(uuid(), t,
                  [{ type: 'paragraph', text: b }]);
                const docA = await mk('Format Probe Alpha',
                  'needle FORMAT-PROBE-ALPHA-9e3c in alpha body');
                await mk('Format Probe Beta',
                  'needle FORMAT-PROBE-BETA-5d81 in beta body');
                const docC = await mk('Format Probe Charlie',
                  'needle FORMAT-PROBE-CHARLIE-c72a in charlie body');
                // Delete one doc so the tape must carry a tombstone entry — a
                // deleted:true skeleton WITHOUT ciphertext fields. The audit
                // below must not mistake a live entry for a tombstone.
                await r.store.delete(docC.id);
                const params = await db.getParams();
                const rows = [];
                for (const { id } of await db.listDocs()) rows.push(await db.getDoc(id));
                const kbText = new TextDecoder().decode(
                  serializeKB(exportKB(params, rows)));
                const tape = await buildTape(params, db);
                const tapeText = new TextDecoder().decode(serializeTape(tape));
                // Pre-report decodability of every payload blob using the app's
                // own fromBase64; the Python audit cross-checks the same set via
                // stdlib base64 so the contract is not app-self-agreement only.
                const probe = (s) => {
                  try { return fromBase64(s).byteLength; } catch { return -1; }
                };
                return cb({
                  kb: kbText,
                  tape: tapeText,
                  liveDocs: rows.length,
                  tapeEntries: tape.entries.length,
                  tapeTombstones: tape.entries.filter((e) => e.deleted).length,
                  sizes: {
                    wsSalt: probe(params.salt),
                    verIv: probe(params.verifier.iv),
                    verData: probe(params.verifier.data),
                  },
                });
              })().catch((e) => cb({ error: 'page: ' + String(e) }));
            """), default=str))
            check("manual 4.1/format: format probe ran on a fresh profile (non-vacuous)",
                  isinstance(_fmt19, dict) and "error" not in _fmt19,
                  str(_fmt19)[:200])
            _kb_obj = json.loads(_fmt19.get("kb") or "")
            _tp_obj = json.loads(_fmt19.get("tape") or "")
            check("manual 4.1/format: .kb backup is valid JSON (备份格式 .kb JSON)",
                  isinstance(_kb_obj, dict), str(_kb_obj)[:120])
            check("manual 4.1/format: .kbsync sync file is valid JSON text "
                  "(同步格式 .kbsync 文本)",
                  isinstance(_tp_obj, dict), str(_tp_obj)[:120])

            def _b64u19(s):
                # URL-safe base64 with the app's unpadded alphabet (stdlib
                # cross-check, independent of src/crypto.js).
                import base64 as _b
                try:
                    return _b.urlsafe_b64decode(str(s) + "=" * (-len(str(s)) % 4))
                except Exception:
                    return None

            check("manual 4.1/format: .kb has the documented top-level schema "
                  "(app/format/exportedAt/workspace/documents)",
                  {"app", "format", "exportedAt", "workspace", "documents"}
                  <= set(_kb_obj.keys())
                  and _kb_obj.get("app") == "local-kb"
                  and _kb_obj.get("format") == 1,
                  str(sorted(_kb_obj.keys())))
            _ws19 = _kb_obj.get("workspace") or {}
            check("manual 4.1/format: .kb workspace carries the cleartext public "
                  "param set claimed by 4.1/4.2 (name/version/salt/iterations/"
                  "verifier)",
                  {"name", "version", "salt", "iterations", "verifier"}
                  == set(_ws19.keys())
                  and isinstance(_ws19.get("name"), str)
                  and _ws19.get("version") == 1
                  and _ws19.get("iterations") == 310000,
                  str(_ws19)[:140])
            _docs19 = _kb_obj.get("documents") or []
            check("manual 4.1/format: .kb doc entries expose ONLY ciphertext keys "
                  "(no title/blocks plaintext in the schema)",
                  len(_docs19) >= 2 and all(
                      set(d.keys()) == {"id", "dekIv", "dekData", "iv", "data",
                                        "createdAt", "updatedAt"}
                      for d in _docs19),
                  str([sorted(d.keys()) for d in _docs19[:2]]))
            _blobok19 = [d for d in _docs19 if all(
                _b64u19(d.get(f)) is not None
                for f in ("dekIv", "dekData", "iv", "data"))]
            _lenok19 = all(
                len(_b64u19(d.get("dekIv"))) == 12
                and len(_b64u19(d.get("iv"))) == 12
                and len(_b64u19(d.get("dekData"))) >= 44   # 32B DEK + 12B iv + GCM tag
                and len(_b64u19(d.get("data"))) >= 16      # ≥ the AES-GCM auth tag
                for d in _docs19)
            check("manual 4.1/format: every .kb ciphertext field is decodeable "
                  "base64url with AES-GCM shapes (dekIv/iv 12B, dekData ≥44B, data ≥16B)",
                  len(_blobok19) == len(_docs19) and _lenok19,
                  f"docs={len(_docs19)} decodable={len(_blobok19)}")
            _ids19 = [d.get("id") for d in _docs19]
            check("manual 4.1/format: .kb document ids are unique",
                  len(set(_ids19)) == len(_ids19),
                  f"ids={len(_ids19)} unique={len(set(_ids19))}")
            _wssalt19 = _b64u19(_ws19.get("salt"))
            _ver19 = _ws19.get("verifier") or {}
            check("manual 4.1/format: workspace salt + verifier are decodable "
                  "base64url (salt 16 random bytes, 仅密文 at field level)",
                  _wssalt19 is not None and len(_wssalt19) == 16
                  and _b64u19(_ver19.get("iv")) is not None
                  and _b64u19(_ver19.get("data")) is not None,
                  f"salt={str(_ws19.get('salt'))[:16]}…")
            check("manual 4.1/format: the .kb file itself leaks no document "
                  "plaintext (needles absent from the raw JSON text)",
                  all(m not in (_fmt19.get("kb") or "") for m in
                      ["FORMAT-PROBE-ALPHA-9e3c", "FORMAT-PROBE-BETA-5d81",
                       "FORMAT-PROBE-CHARLIE-c72a", "Format Probe Alpha"]),
                  "needle leaked into .kb")

            check("manual 4.1/format: .kbsync has the documented tape schema "
                  "(app/type/format/createdAt/workspace/entries)",
                  {"app", "type", "format", "createdAt", "workspace", "entries"}
                  <= set(_tp_obj.keys())
                  and _tp_obj.get("app") == "local-kb"
                  and _tp_obj.get("type") == "sync-tape"
                  and _tp_obj.get("format") == 1,
                  str(sorted(_tp_obj.keys())))
            _ents19 = _tp_obj.get("entries") or []
            _live19 = [e for e in _ents19 if not e.get("deleted")]
            _dead19 = [e for e in _ents19 if e.get("deleted")]
            check("manual 4.1/format: live tape entries carry the same four "
                  "decodable ciphertext blobs as the .kb docs",
                  len(_live19) >= 1 and all(
                      {"id", "dekIv", "dekData", "iv", "data",
                       "createdAt", "updatedAt", "deleted"} == set(e.keys())
                      and all(_b64u19(e.get(f)) is not None
                              for f in ("dekIv", "dekData", "iv", "data"))
                      for e in _live19),
                  f"live={len(_live19)} shapes={[sorted(e.keys()) for e in _live19[:1]]}")
            check("manual 4.1/format: tombstoned tape entries are ciphertext-free "
                  "delete markers (deleted:true, no payload fields)",
                  len(_dead19) >= 1 and all(
                      set(e.keys()) == {"id", "updatedAt", "deleted"}
                      and e.get("deleted") is True
                      and all(f not in e for f in ("dekIv", "dekData", "iv", "data"))
                      for e in _dead19),
                  f"tombstones={len(_dead19)} shapes={[sorted(e.keys()) for e in _dead19[:1]]}")
            check("manual 4.1/format: the .kbsync file itself leaks no document "
                  "plaintext (needles absent from the raw JSON text)",
                  all(m not in (_fmt19.get("tape") or "") for m in
                      ["FORMAT-PROBE-ALPHA-9e3c", "FORMAT-PROBE-BETA-5d81",
                       "FORMAT-PROBE-CHARLIE-c72a", "Format Probe Alpha",
                       "Format Probe Beta", "Format Probe Charlie"]),
                  "needle leaked into .kbsync")
        finally:
            _d19.quit()

        # ---- 用户手册 3.8 step 5 / Q5: "双方互相交换一次后，两台机器的文档
        # 集合与各文档最新版本一致" and "分别在两台机器'同步引出/同步引入'
        # .kbsync 文件各一次，即可双向收敛（含删除语义）". Every existing sync
        # block is ONE-way (A edits -> B receives; A deletes -> B receives);
        # none proves the FULL symmetric exchange where BOTH machines diverge
        # independently and reconverge. This block drives that end-to-end on
        # scratch IndexedDB peers of one shared seed workspace (main vault
        # untouched): both machines pull the identical seed tape; A edits doc1
        # and adds doc3, B (after a beat, so its edit is strictly newer) edits
        # doc1 differently, deletes doc2 and adds doc4; then A->B tape, then
        # B->A tape. After ONE mutual exchange both must hold the SAME doc set
        # (doc2 expelled on both, doc3/doc4 arrived on both) and per-doc the
        # SAME latest version — byte-identical ciphertext, LWW winner on both.
        # The main driver was last pointed at the torn-down ephemeral stdlib
        # server of the manual 2.1/Q2 block, so /idb.js + /src/* would resolve
        # against a dead origin here. Hop back to the live demo origin first;
        # the app reloads to the lock screen, which nothing below depends on.
        driver.get(BASE + "/")
        _mut20 = json.loads(json.dumps(driver.execute_async_script("""
          const cb = arguments[arguments.length - 1];
          (async () => {
            const { IndexedDBBackend } = await import('/idb.js');
            const { createKb, openKb } = await import('/src/factory.js');
            const { buildTape, applyTapeFile } = await import('/src/syncfile.js');
            const { uuid } = await import('/src/util.js');
            const PW = 'correct horse battery staple';
            const snooze = (ms) => new Promise((r) => setTimeout(r, ms));
            const textOf = (d) => (d.blocks || []).map((x) => x.text).join(' ');
            const snap = async (peer, id) => {
              const e = await peer.p.getDoc(id);
              const d = await peer.pr.store.get(id);
              return d ? {
                title: d.title,
                blocksText: textOf(d),
                updatedAt: d.updatedAt,
                iv: e ? e.iv : null, data: e ? e.data : null,
                dekIv: e ? e.dekIv : null, dekData: e ? e.dekData : null,
              } : null;
            };
            // One shared seed workspace: two docs, exported once, so both
            // machines start byte-identical from the same tape.
            const seed = new IndexedDBBackend('local-kb-c20-seed');
            const sr = await createKb(seed, PW, 'mutual-ws');
            const doc1 = await sr.store.create(uuid(), 'Doc One',
                        [{ type: 'paragraph', text: 'MUTUAL-SEED-ONE-c03d' }]);
            const doc2 = await sr.store.create(uuid(), 'Doc Two',
                        [{ type: 'paragraph', text: 'MUTUAL-SEED-TWO-5b91' }]);
            const seedParams = await seed.getParams();
            const seedTape = await buildTape(seedParams, seed);
            const mkPeer = async (name) => {
              const p = new IndexedDBBackend(name);
              await p.saveParams(seedParams);
              const pr = await openKb(p, PW);
              return { p, pr };
            };
            const a = await mkPeer('local-kb-c20-a');
            const b = await mkPeer('local-kb-c20-b');
            if (!a.pr.ok || !b.pr.ok) return cb({ error: 'peer-unlock' });
            await applyTapeFile(a.p, seedTape);
            await applyTapeFile(b.p, seedTape);
            const aInit = (await a.pr.store.list()).map((d) => d.id).sort();
            const bInit = (await b.pr.store.list()).map((d) => d.id).sort();
            // ---- Diverge (each machine edits independently).
            const d1a = await a.pr.store.get(doc1.id);
            await a.pr.store.edit(doc1.id, (d1a.title || '') + ' (A)',
                (d1a.blocks || []).concat({ type: 'paragraph', text: 'MUTUAL-A-EDIT-84f2' }));
            const doc3 = await a.pr.store.create(uuid(), 'Doc Three',
                        [{ type: 'paragraph', text: 'MUTUAL-NEW-A-99ad' }]);
            await snooze(200); // B's edit must be strictly NEWER than A's
            const d1b0 = await b.pr.store.get(doc1.id);
            await b.pr.store.edit(doc1.id, (d1b0.title || '') + ' (B)',
                (d1b0.blocks || []).concat({ type: 'paragraph', text: 'MUTUAL-B-EDIT-17ae' }));
            await b.pr.store.delete(doc2.id);
            const doc4 = await b.pr.store.create(uuid(), 'Doc Four',
                        [{ type: 'paragraph', text: 'MUTUAL-NEW-B-2c77' }]);
            const aPre = new Set((await a.pr.store.list()).map((d) => d.id));
            const bPre = new Set((await b.pr.store.list()).map((d) => d.id));
            const d1aP = await a.pr.store.get(doc1.id);
            const d1bP = await b.pr.store.get(doc1.id);
            const diverge = aPre.has(doc3.id) && bPre.has(doc4.id)
                            && !bPre.has(doc2.id)
                            && textOf(d1aP).includes('MUTUAL-A-EDIT-84f2')
                            && textOf(d1bP).includes('MUTUAL-B-EDIT-17ae');
            const divDoc1 = JSON.stringify({ title: d1aP.title, blocks: d1aP.blocks })
                            !== JSON.stringify({ title: d1bP.title, blocks: d1bP.blocks });
            // ---- One mutual exchange: A out -> B in, then B out -> A in.
            await applyTapeFile(b.p, await buildTape(seedParams, a.p));
            await applyTapeFile(a.p, await buildTape(seedParams, b.p));
            // ---- Convergence on both machines.
            const finalAIds = (await a.pr.store.list()).map((d) => d.id).sort();
            const finalBIds = (await b.pr.store.list()).map((d) => d.id).sort();
            const setsConverge =
              JSON.stringify(finalAIds) === JSON.stringify(finalBIds)
              && finalAIds.includes(doc3.id) && finalAIds.includes(doc4.id)
              && !finalAIds.includes(doc2.id);
            const d2A = await snap(a, doc2.id);
            const d2B = await snap(b, doc2.id);
            const tombsA = (await a.p.listTombstones()).filter((t) => t.id === doc2.id).length;
            const tombsB = (await b.p.listTombstones()).filter((t) => t.id === doc2.id).length;
            const tA2 = (await buildTape(seedParams, a.p)).entries.find((e) => e.id === doc2.id);
            const tB2 = (await buildTape(seedParams, b.p)).entries.find((e) => e.id === doc2.id);
            const deleteConverged = d2A === null && d2B === null
                                    && tombsA >= 1 && tombsB >= 1
                                    && tA2 && tA2.deleted === true
                                    && tB2 && tB2.deleted === true;
            const d1A = await snap(a, doc1.id);
            const d1B = await snap(b, doc1.id);
            const d3A = await snap(a, doc3.id);
            const d3B = await snap(b, doc3.id);
            const d4A = await snap(a, doc4.id);
            const d4B = await snap(b, doc4.id);
            const blocksTextA = d1A.blocksText || '';
            const blocksTextB = d1B.blocksText || '';
            const sameCipher = d1A && d1B && d1A.iv === d1B.iv
                               && d1A.data === d1B.data
                               && d1A.dekIv === d1B.dekIv
                               && d1A.dekData === d1B.dekData;
            const sameTs = d1A && d1B && d1A.updatedAt === d1B.updatedAt;
            // B's edit is the newer of the two competing versions (60ms snooze
            // + PBKDF2 unlocks), so LWW must land on the B version on BOTH
            // machines — and the superseded A edit is dropped consistently.
            const lwwWinner = !!(d1A && d1B)
                              && sameCipher && sameTs
                              && (d1B.title || '').includes(' (B)')
                              && (d1A.title || '').includes(' (B)')
                              && blocksTextB.includes('MUTUAL-B-EDIT-17ae')
                              && blocksTextA.includes('MUTUAL-B-EDIT-17ae')
                              && !blocksTextA.includes('MUTUAL-A-EDIT-84f2')
                              && !blocksTextB.includes('MUTUAL-A-EDIT-84f2');
            // A's new doc3 and B's new doc4 both arrive on both machines with
            // byte-identical ciphertext (the receiving side stores the tape
            // entry's exact blobs, so both converge to identical bytes).
            const doc3Converged = !!(d3A && d3B)
                                  && d3A.iv === d3B.iv && d3A.data === d3B.data
                                  && d3A.dekIv === d3B.dekIv && d3A.dekData === d3B.dekData
                                  && d3A.title === d3B.title;
            const doc4Converged = !!(d4A && d4B)
                                  && d4A.iv === d4B.iv && d4A.data === d4B.data
                                  && d4A.dekIv === d4B.dekIv && d4A.dekData === d4B.dekData
                                  && d4A.title === d4B.title;
            return cb({
              error: null,
              initialSame: JSON.stringify(aInit) === JSON.stringify(bInit),
              diverge, divDoc1,
              divOrder: d1bP.updatedAt > d1aP.updatedAt,
              setsConverge,
              doc3Converged, doc4Converged,
              deleteConverged,
              lwwWinner,
              finalAIds, finalBIds,
            });
          })().catch((e) => cb({ error: String(e) }));
        """), default=str))
        _m = _mut20 if isinstance(_mut20, dict) else {}
        check("sync: mutual exchange ends with IDENTICAL doc sets on both machines — "
              "A's doc3 and B's doc4 both across, B's deleted doc2 absent on both "
              "(manual 3.8 step 5 / Q5)",
              _m.get("diverge") is True and _m.get("divDoc1") is True
              and _m.get("initialSame") is True and _m.get("setsConverge") is True,
              "initial-same=%s diverge=%s sets-converge=%s idsA=%s idsB=%s"
              % (_m.get("initialSame"), _m.get("diverge"), _m.get("setsConverge"),
                 _m.get("finalAIds"), _m.get("finalBIds")))
        check("sync: delete convergence — doc2 absent on both machines, tombstoned "
              "on both, both current tapes carry deleted:true (manual 3.8 step 5 / Q5)",
              _m.get("deleteConverged") is True,
              "deleteConverged=%s" % (_m.get("deleteConverged"),))
        check("sync: LWW lands on B's strictly-newer doc1 version on BOTH machines — "
              "byte-identical ciphertext + updatedAt, A's superseded edit dropped "
              "consistently (manual 3.8 step 5 / Q5)",
              _m.get("lwwWinner") is True and _m.get("divOrder") is True,
              "b-newer=%s lww-winner=%s" % (_m.get("divOrder"), _m.get("lwwWinner")))
        check("sync: each machine's own new doc crosses to the other in ciphertext "
              "and both hold byte-identical copies (manual 3.8 step 5 / Q5)",
              _m.get("doc3Converged") is True and _m.get("doc4Converged") is True,
              "doc3-converged=%s doc4-converged=%s" % (_m.get("doc3Converged"),
                                                        _m.get("doc4Converged")))
        if _m.get("error"):
            check("sync: mutual-exchange run completed without error (manual 3.8/Q5)",
                  False, "in-page error: %s" % (_m.get("error"),))

        # ---- 用户手册 3.2: "页面在指定时间内没有操作（移动鼠标、按键、滚动等）
        # 会自动锁定" — the activity-POSTPONE half. The earlier idle block pinned
        # the lock *firing* after a quiet 3 s window plus auto-save; it never
        # proved that real user activity postpones the timer. This block, on a
        # self-contained scratch profile (Cycle 13's separate-driver idiom),
        # creates a vault, then shrinks the idle window to 2.5 s and feeds each
        # of the manual's named activity kinds ("移动鼠标、按键、滚动等" — plus
        # press and touch; public/main.js:538 arms the timer on
        # mousemove/mousedown/keydown/scroll/touchstart) at a cadence that keeps
        # re-arming the timer. Each kind gets its own slot: dispatch, 1.3 s,
        # dispatch again (before the 2.5 s deadline), 1.3 s — a full idle
        # window elapses without the lock firing, which is exactly the postpone
        # guarantee. A final no-activity control shows the lock still fires on
        # its own once the user goes quiet.
        _opts21 = Options()
        _opts21.headless = True
        _opts21.binary_location = fb
        _opts21.set_preference("dom.indexedDB.enabled", True)
        _d21 = webdriver.Firefox(options=_opts21)
        try:
            _d21.get(BASE + "/")
            wait_for(_d21, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d21.find_element(By.ID, "wsname").send_keys("cycle21-idle-ws")
            _d21.find_element(By.ID, "pw2").send_keys("cycle21 idle pw")
            _d21.find_element(By.ID, "pw2b").send_keys("cycle21 idle pw")
            _d21.find_element(By.ID, "btn-create").click()
            wait_for(_d21, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            wait_for(_d21, EC.visibility_of_element_located((By.ID, "btn-new")), 12)
            check("idle postpone: fresh scratch vault unlocked (non-vacuous)",
                  _d21.find_element(By.ID, "btn-lock").is_displayed(), True)
            _d21.execute_script("window.LOCAL_KB_IDLE_MS = 2500;")

            def _poke21(evt):
                _d21.execute_script(
                    "document.dispatchEvent(new Event(arguments[0], { bubbles: true }));",
                    evt)

            for _evt in ["mousemove", "mousedown", "keydown", "scroll", "touchstart"]:
                _poke21(_evt)
                time.sleep(1.3)
                _poke21(_evt)  # re-arm before the 2.5 s deadline
                time.sleep(1.3)
                check("idle postpone: %s activity keeps the app unlocked across a "
                      "full idle window (manual 3.2)" % _evt,
                      _d21.find_element(By.ID, "view-app").is_displayed(), True)
            # Five kinds × one full 2.5 s window each: the app is still unlocked,
            # i.e. activity re-armed the timer for the whole run (~5 windows).
            check("idle postpone: several consecutive idle windows elapse without a "
                  "lock while activity continues (manual 3.2)",
                  _d21.find_element(By.ID, "view-app").is_displayed(), True)

            # Control: stop every form of activity; the armed timer now fires on
            # its own (the firing half already pinned above, re-observed here so
            # the postpone proof cannot be vacuous).
            time.sleep(4.0)
            wait_for(_d21, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            check("idle postpone: with no activity the lock still fires (manual 3.2)",
                  True, True)
            pw_held = _d21.execute_script("return sessionStorage.getItem('local-kb-pw');")
            check("idle postpone: post-lock session password is dropped (manual 3.2)",
                  pw_held is None or pw_held == "", str(pw_held))
            check("idle postpone: inactivity lock text appears in the log (manual 3.2)",
                  "auto-locked" in _d21.find_element(By.ID, "log").text.lower(),
                  _d21.find_element(By.ID, "log").text)
        finally:
            _d21.quit()

        # ---- 用户手册 4.1 (config table): "主密码 … ≥ 8 字符，建议 ≥ 12 字符"
        # applied to the 3.9 修改主密码 (re-key) flow. The create-path floor is
        # already pinned (line 186); public/main.js:316 enforces the SAME floor
        # on the re-key path (`if (next.length < 8) return log('New password
        # must be at least 8 characters.', true);`), but no existing check ever
        # drives a too-short NEW password through the re-key card. This block,
        # on its own self-contained scratch profile (Cycle 13/21 idiom), creates
        # a vault, attempts a re-key whose new password is exactly one character
        # under the floor, and asserts: the UI refuses it (exact gate message,
        # no "changed" receipt), the app stays unlocked, and the OLD password
        # still unlocks — i.e. no rotation happened. A control then re-keys with
        # an 8+-char password and must succeed, proving the refusal was
        # length-specific, not a broken flow.
        _opts22 = Options()
        _opts22.headless = True
        _opts22.binary_location = fb
        _opts22.set_preference("dom.indexedDB.enabled", True)
        _d22 = webdriver.Firefox(options=_opts22)
        try:
            _d22.get(BASE + "/")
            wait_for(_d22, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _OLD22 = "cycle22 old pw 2026"
            _d22.find_element(By.ID, "wsname").send_keys("cycle22-rekey-floor")
            _d22.find_element(By.ID, "pw2").send_keys(_OLD22)
            _d22.find_element(By.ID, "pw2b").send_keys(_OLD22)
            _d22.find_element(By.ID, "btn-create").click()
            wait_for(_d22, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            wait_for(_d22, EC.presence_of_element_located((By.ID, "rekey-current")), 12)
            check("re-key floor: fresh scratch vault unlocked with re-key card "
                  "(non-vacuous)",
                  _d22.find_element(By.ID, "view-app").is_displayed(), True)

            _d22.find_element(By.ID, "rekey-current").send_keys(_OLD22)
            _d22.find_element(By.ID, "rekey-new").send_keys("short12")  # 7 chars
            _d22.find_element(By.ID, "rekey-confirm").send_keys("short12")
            _d22.find_element(By.ID, "btn-rekey").click()
            time.sleep(1.0)
            _log22 = _d22.find_element(By.ID, "log").text
            check("re-key floor: 7-character new password refused with the ≥8 gate "
                  "message (manual 4.1/3.9)",
                  "at least 8" in _log22 and "Master password changed" not in _log22,
                  _log22)
            check("re-key floor: rejected re-key leaves the app unlocked",
                  _d22.find_element(By.ID, "view-app").is_displayed(), True)

            # No rotation happened: the OLD password must still unlock after a
            # reload. (A botched gate could silently rotate the key, which a
            # UI-layer refusal would otherwise mask.)
            _d22.refresh()
            wait_for(_d22, EC.visibility_of_element_located((By.ID, "view-unlock")), 12)
            _d22.find_element(By.ID, "pw").send_keys(_OLD22)
            _d22.find_element(By.ID, "btn-unlock").click()
            wait_for(_d22, EC.visibility_of_element_located((By.ID, "view-app")), 12)
            check("re-key floor: old password still unlocks after the refusal "
                  "(key not rotated)",
                  _d22.find_element(By.ID, "view-app").is_displayed(), True)

            # Control: an 8+-char new password must pass the SAME card, proving
            # the refusal above was length-specific.
            _d22.find_element(By.ID, "rekey-current").send_keys(_OLD22)
            _d22.find_element(By.ID, "rekey-new").send_keys("cycle22 new pw 9191")
            _d22.find_element(By.ID, "rekey-confirm").send_keys("cycle22 new pw 9191")
            _d22.find_element(By.ID, "btn-rekey").click()
            time.sleep(2.0)
            _log22 = _d22.find_element(By.ID, "log").text
            check("re-key floor: control — 8+-char new password re-keys cleanly "
                  "(manual 4.1/3.9)",
                  "Master password changed" in _log22, _log22)
        finally:
            _d22.quit()

        # ---- 用户手册 3.3 (文档持久化): "保存即在本机完成端到端加密并写
        # 入本地存储；关闭页面、重启浏览器后再次解锁即可看到全部文档"
        # (用户手册.txt:123-124). Every earlier persistence check only calls
        # `refresh()` — the SAME browser process. This block is the first TRUE
        # process-boundary proof: a scratch Firefox profile lives under $HOME
        # (snap confinement rejects `-profile` dirs on /tmp, which is why the
        # all-but-unused `fresh_profile()` helper above would never work), a
        # vault + two docs are written through the UI, the driver fully QUITS,
        # and a brand-new Firefox process launched on the SAME `-profile` dir
        # must unlock the vault with both docs listed again. IndexedDB web
        # files are also asserted on disk after quit, and AC2 (no plaintext in
        # IndexedDB) is re-verified on the true-restart path.
        import shutil as _shutil23
        import glob as _glob23
        import tempfile as _tempfile23

        _rdir23 = _tempfile23.mkdtemp(prefix="ffkb-cycle24-",
                                      dir=os.path.expanduser("~"))
        with open(os.path.join(_rdir23, "prefs.js"), "w") as _f23:
            _f23.write("# Mozilla User Preferences\n\n")

        def _mk23():
            _o = Options()
            _o.headless = True
            _o.binary_location = fb
            _o.add_argument("-profile")
            _o.add_argument(_rdir23)
            _d = webdriver.Firefox(options=_o)
            _d.set_page_load_timeout(20)
            return _d

        _PW23 = "cycle24 restart pw 2026"
        try:
            _a23 = _mk23()
            try:
                _a23.get(BASE + "/")
                wait_for(_a23, EC.visibility_of_element_located(
                    (By.ID, "view-unlock")), 12)
                _a23.find_element(By.ID, "wsname").send_keys("cycle24-restart-ws")
                _a23.find_element(By.ID, "pw2").send_keys(_PW23)
                _a23.find_element(By.ID, "pw2b").send_keys(_PW23)
                _a23.find_element(By.ID, "btn-create").click()
                wait_for(_a23, EC.visibility_of_element_located(
                    (By.ID, "view-app")), 12)
                wait_for(_a23, EC.visibility_of_element_located(
                    (By.ID, "btn-new")), 12)
                check("restart persist: fresh restart-profile vault unlocked "
                      "(non-vacuous)",
                      _a23.find_element(By.ID, "view-app").is_displayed(), True)
                for _ti23, _bo23 in [("CYCLE24-DOC-A", "cycle24 alpha payload "
                                      "raven-9"), ("CYCLE24-DOC-B",
                                                   "cycle24 beta payload fox-3")]:
                    _a23.find_element(By.ID, "btn-new").click()
                    time.sleep(1.0)
                    _t23 = _a23.find_element(By.ID, "editor-title")
                    _t23.clear()
                    _t23.send_keys(_ti23)
                    _bd23 = _a23.find_element(By.ID, "editor-area")
                    _bd23.clear()
                    _bd23.send_keys(_bo23)
                    _a23.find_element(By.ID, "btn-save").click()
                    time.sleep(1.2)
                check("restart persist: two docs saved before the process "
                      "boundary (manual 3.3)",
                      len(_a23.find_elements(By.CSS_SELECTOR,
                                             "#doc-list .doc-row")) == 2, True)
            finally:
                _a23.quit()

            # The write must exist as IndexedDB web files in the reused
            # profile directory, not merely in the live page's memory.
            _stor23 = os.path.join(_rdir23, "storage", "default")
            _idb23 = bool(_glob23.glob(os.path.join(_stor23, "**", "*"),
                                       recursive=True))
            check("restart persist: IndexedDB web files persisted to the "
                  "profile dir after quit (manual 3.3)",
                  _idb23, _stor23)

            # TRUE restart: a brand-new Firefox process on the SAME profile dir.
            _b23 = _mk23()
            try:
                _b23.get(BASE + "/")
                wait_for(_b23, EC.visibility_of_element_located(
                    (By.ID, "view-unlock")), 12)
                check("restart persist: post-relaunch page shows only the "
                      "unlock view (manual 3.3)",
                      _b23.find_element(By.ID, "view-unlock").is_displayed(),
                      True)
                _b23.find_element(By.ID, "pw").send_keys(_PW23)
                _b23.find_element(By.ID, "btn-unlock").click()
                wait_for(_b23, EC.visibility_of_element_located(
                    (By.ID, "view-app")), 12)
                check("restart persist: vault re-unlocks with the original "
                      "password after the process boundary (manual 3.3)",
                      _b23.find_element(By.ID, "view-app").is_displayed(), True)
                _rows23 = _b23.find_elements(
                    By.CSS_SELECTOR, "#doc-list .doc-row")
                _txts23 = [r.text for r in _rows23]
                check("restart persist: ALL docs visible again after closing "
                      "the page and restarting the browser (manual 3.3)",
                      len(_rows23) == 2
                      and any("CYCLE24-DOC-A" in t for t in _txts23)
                      and any("CYCLE24-DOC-B" in t for t in _txts23),
                      _txts23)
                check("restart persist: workspace name survived the restart "
                      "too (manual 3.3)",
                      "cycle24-restart-ws"
                      in _b23.find_element(By.ID, "ws-label").text,
                      _b23.find_element(By.ID, "ws-label").text)
                _flat23 = flatten_dump(get_db_dump(_b23))
                check("restart persist: IndexedDB still holds only ciphertext "
                      "on the true-restart path (AC2)",
                      all(s not in _flat23 for s in ["raven-9", "fox-3",
                                                     "CYCLE24-DOC-A"]),
                      _flat23[:200])
            finally:
                _b23.quit()
            _shutil23.rmtree(_rdir23, ignore_errors=True)
        finally:
            _shutil23.rmtree(_rdir23, ignore_errors=True)

        # ---- 用户手册 4.3 (存储位置): "浏览器模式：数据保存在浏览器
        # IndexedDB 中（数据库名如 local-kb）" (用户手册.txt:220). Every earlier
        # IndexedDB check opens the database via the hardcoded string 'local-kb'
        # inside get_db_dump(), so none ever proves that the browser actually
        # REGISTERS a database under that documented name — indexedDB.databases()
        # is unused anywhere in the suite. The app reaches IndexedDB only inside
        # unlock()/createVault() (public/main.js:424/441), so a freshly loaded
        # page must register zero databases (non-vacuous), and once a vault + a
        # saved doc exist through the real UI, exactly one database — named
        # 'local-kb' — must be registered, carry the documented
        # workspace/docs/meta object stores, and hold only ciphertext.
        _opts25 = Options()
        _opts25.headless = True
        _opts25.binary_location = fb
        _opts25.set_preference("dom.indexedDB.enabled", True)
        _d25 = webdriver.Firefox(options=_opts25)

        def _dbs25():
            return _d25.execute_async_script("""
            const cb = arguments[arguments.length - 1];
            indexedDB.databases().then((dbs) => cb(dbs), () => cb([]));
            """)

        try:
            _d25.get(BASE + "/")
            wait_for(_d25, EC.visibility_of_element_located(
                (By.ID, "view-unlock")), 12)
            _dbg25 = _dbs25()
            check("manual 4.3: fresh origin registers no IndexedDB database "
                  "before a vault exists (non-vacuous)",
                  isinstance(_dbg25, list) and len(_dbg25) == 0, _dbg25)
            _d25.find_element(By.ID, "wsname").send_keys("cycle25-dbname-ws")
            _d25.find_element(By.ID, "pw2").send_keys("cycle25 dbname pw 2026")
            _d25.find_element(By.ID, "pw2b").send_keys("cycle25 dbname pw 2026")
            _d25.find_element(By.ID, "btn-create").click()
            wait_for(_d25, EC.visibility_of_element_located(
                (By.ID, "view-app")), 12)
            wait_for(_d25, EC.visibility_of_element_located(
                (By.ID, "btn-new")), 12)
            _d25.find_element(By.ID, "btn-new").click()
            time.sleep(1.0)
            _t25 = _d25.find_element(By.ID, "editor-title")
            _t25.clear()
            _t25.send_keys("CYCLE25-DOC")
            _bd25 = _d25.find_element(By.ID, "editor-area")
            _bd25.clear()
            _bd25.send_keys("cycle25 ember payload lynx-7")
            _d25.find_element(By.ID, "btn-save").click()
            time.sleep(1.5)
            _dbn25 = [_b.get("name") for _b in _dbs25()]
            check("manual 4.3: the browser registers exactly one IndexedDB "
                  "database named 'local-kb' for the vault (数据库名如 "
                  "local-kb)",
                  len(_dbn25) == 1 and _dbn25[0] == "local-kb", _dbn25)
            _dump25 = get_db_dump(_d25)
            _stores25 = (sorted(_dump25.keys())
                         if isinstance(_dump25, dict) else [])
            check("manual 4.3: the local-kb database carries the "
                  "workspace/docs/meta object stores",
                  "workspace" in _stores25 and "docs" in _stores25
                  and "meta" in _stores25, _stores25)
            _flat25 = flatten_dump(_dump25)
            check("manual 4.3: the local-kb database persists only ciphertext "
                  "(AC2 on the documented storage location)",
                  all(s not in _flat25 for s in ["lynx-7", "CYCLE25-DOC"]),
                  _flat25[:200])
        finally:
            _d25.quit()

    finally:
        driver.quit()

    if failures:
        print("\nBROWSER E2E FAILURES:", failures)
        sys.exit(1)
    print("\nALL BROWSER E2E CHECKS PASSED (AC1,AC2,AC3,AC4,AC5,AC6 minimal)")


if __name__ == "__main__":
    main()
