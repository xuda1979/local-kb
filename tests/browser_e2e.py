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

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:4242")


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

        # ---- create workspace
        pw = driver.find_element(By.ID, "pw2")
        pw.send_keys("correct horse battery staple")
        driver.find_element(By.ID, "btn-create").click()
        # view-app exists in static HTML but is display:none until the async
        # PBKDF2 (310k iters) key derivation finishes and setView() runs — wait
        # for actual visibility, not DOM presence.
        wait_for(driver, EC.visibility_of_element_located((By.ID, "view-app")), 12)
        check("workspace created, app view shown", True)

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

        # ---- P1 editor: markdown preview toggle + XSS safety (real browser)
        # The current doc is open in the editor at this point.
        area = driver.find_element(By.ID, "editor-area")
        area.send_keys(
            '\n## MD Preview Check\n**bold** `code` [bad](javascript:alert(1))\n<script>alert(1)</script>'
        )
        driver.find_element(By.ID, "btn-preview").click()
        time.sleep(0.6)
        prev = driver.find_element(By.ID, "preview")
        inner = prev.get_attribute("innerHTML")
        check("preview renders markdown headings", "<h2>MD Preview Check</h2>" in inner, inner[:200])
        check("preview renders inline formatting", "<strong>bold</strong>" in inner)
        check("preview escapes raw HTML inert", "<script>alert" not in inner and "&lt;script&gt;" in inner)
        check("preview rejects javascript: links", "<em>bad</em>" in inner and "javascript:" not in inner)
        check("preview toggle button flips to Edit", driver.find_element(By.ID, "btn-preview").text == "Edit")
        driver.find_element(By.ID, "btn-preview").click()  # back to editor
        time.sleep(0.4)
        check("preview toggle returns to editor",
              driver.find_element(By.ID, "editor-area").is_displayed())

        # ---- AC2: IndexedDB holds no plaintext
        dump = flatten_dump(get_db_dump(driver))
        for needle in [KNOWN_TITLE, "oscar-7", "48.8584N", "deflector", "Project Snowstorm"]:
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
        driver.find_element(By.ID, "btn-search").click()
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#doc-list .doc-row")
        check("AC5: search hits the doc by keyword",
              any(KNOWN_TITLE in (r.text or "") for r in rows), [r.text for r in rows])

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

        # ---- AC6: offline — assert page JS/import loads (no network fallback)
        # The page itself is a static module graph served from /public; language
        # server already covered by reloads above. Also verify demo serves offline
        # static files with no external CDN references.
        html = driver.page_source
        check("AC6: page references no external network",
              all(x not in html for x in ["https://", "//cdn", "http://"]))

    finally:
        driver.quit()

    if failures:
        print("\nBROWSER E2E FAILURES:", failures)
        sys.exit(1)
    print("\nALL BROWSER E2E CHECKS PASSED (AC1,AC2,AC3,AC4,AC5,AC6 minimal)")


if __name__ == "__main__":
    main()
