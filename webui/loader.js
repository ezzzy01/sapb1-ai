// SAP B1 AI: people sign in on the shared sign-in page (/portal/login, src/sapb1_ai/portal.py), which sends them to
// the company they chose. Each company has its own Open WebUI, so this page only ever shows that company. Open WebUI
// loads /static/loader.js on every page; this script:
//  - sends Open WebUI's own sign-in page back to the shared one once the shared sign-in has ended (Open WebUI signs
//    people in by itself while it is valid);
//  - keeps the model menu fixed (each site has just its own company);
//  - shows the home dashboard (/sapb1/home, src/sapb1_ai/dashboard.py) whenever the start screen is shown, until the
//    person starts a chat (New Chat on the left, or a button on the dashboard); opening an earlier chat only hides it.
//    A question asked on the dashboard opens the chat beside it (split view). "Dashboard" in the left sidebar brings it back (dashboard only).
(() => {
  const style = document.createElement("style");
  style.textContent =
    '#model-selector-model-button{pointer-events:none}#model-selector-model-button svg,button[aria-label="Add Model"]{display:none!important}';
  document.head.append(style);

  let checking = false;
  setInterval(async () => {
    if (checking || !location.pathname.startsWith("/auth")) return;
    checking = true;
    try {
      const r = await fetch("/portal/session", { cache: "no-store", credentials: "include" });
      const signedIn = r.ok && (await r.json()).signed_in;
      if (!signedIn) location.replace("/portal/login");
    } catch (e) {
      location.replace("/portal/login");
    } finally {
      checking = false;
    }
  }, 500);

  // ---------------------------------------------------------------- home dashboard
  // Full: the dashboard covers the start screen. Split: a question asked from the dashboard opens the chat on the right
  // and the dashboard stays on the left, so people can read the answer while looking at the figures.
  // "Dashboard" in the left sidebar (under New Chat and Search) brings it back. On phones the sidebar slides over the
  // page, so the dashboard has its own ☰ button that opens it.
  const home = () => location.pathname === "/" || location.pathname === "";
  const chatPage = () => home() || location.pathname.startsWith("/c/");
  const SPLIT_MIN_WIDTH = 900;  // below this (phones, small windows) a question just opens the chat
  let frame = null, dismissed = false;  // dismissed: the person started a chat; a page reload resets it
  let split = false, openedAt = "", told = "";

  const style2 = document.createElement("style");
  style2.textContent = "html.sapb1-split #chat-container{padding-left:var(--sapb1-dash-w,0px)!important}" +
    // "Close chat" at the top of the chat when it is beside the dashboard
    // an icon button like the chat's own top-right icons (fixed size, so the page's styles cannot enlarge it)
    "#sapb1-close-chat{all:unset;box-sizing:border-box;position:fixed;z-index:38;display:none;align-items:center;justify-content:center;" +
    "right:46px;width:30px;height:30px;border-radius:8px;color:#4b5563;cursor:pointer}#sapb1-close-chat:hover{background:#f3f4f6;color:#111827}" +
    "#sapb1-close-chat:focus-visible{outline:2px solid #0070f2;outline-offset:1px}#sapb1-close-chat svg{width:18px;height:18px;display:block}" +
    "html.dark #sapb1-close-chat{color:#9ca3af}html.dark #sapb1-close-chat:hover{background:#262626;color:#f3f4f6}" +
    "html.sapb1-split #sapb1-close-chat{display:inline-flex}";
  document.head.append(style2);

  const isOpen = () => !!frame && frame.style.display !== "none";
  const sidebarEl = () => document.getElementById("sidebar");
  // The open sidebar slides over the page on phones (below 768px, Open WebUI's "md"); on a PC it pushes the page over
  // (it is position:fixed there too, with the page moved right).
  const sidebarOverlay = () => { const sb = sidebarEl(); return !sb || (getComputedStyle(sb).position === "fixed" && innerWidth < 768); };
  function sidebarRight() {
    const sb = sidebarEl();
    return sb && sb.offsetWidth > 0 && !sidebarOverlay() ? Math.round(sb.getBoundingClientRect().right) : 0;
  }
  function dashWidth(left) { return Math.max(420, Math.round((innerWidth - left) * 0.55)); }
  function place() {
    const left = sidebarRight();
    if (split && innerWidth < SPLIT_MIN_WIDTH) setSplit(false, true);
    const w = split ? dashWidth(left) : innerWidth - left;
    const top = navBottom();
    if (frame) {
      frame.style.left = left + "px"; frame.style.width = w + "px";
      frame.style.top = top ? top + "px" : "var(--sap-shell-h,0px)";
      frame.style.height = top ? `calc(100dvh - ${top}px)` : "calc(100dvh - var(--sap-shell-h,0px))";
    }
    document.documentElement.style.setProperty("--sapb1-dash-w", (split && isOpen() ? w : 0) + "px");
    document.documentElement.classList.toggle("sapb1-split", split && isOpen());
    if (split && isOpen()) {
      let b = document.getElementById("sapb1-close-chat");
      if (!b) {
        b = document.createElement("button");
        b.id = "sapb1-close-chat"; b.type = "button"; b.title = "Close chat (show only the dashboard)";
        b.setAttribute("aria-label", "Close chat");
        b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
        b.onclick = () => { b.blur(); dashboardOnly(); };
        document.body.append(b);
      }
      b.style.top = `calc(${top ? top + "px" : "var(--sap-shell-h,0px)"} + 5px)`;
      // just left of the chat's own icons at the top right (a new chat has one more: Temporary Chat)
      const icons = [...document.querySelectorAll("nav button")].filter((x) => x !== b && x.offsetWidth > 0)
        .map((x) => x.getBoundingClientRect()).filter((r) => r.top < 120 && r.left > left + w);
      const edge = icons.length ? Math.min(...icons.map((r) => r.left)) : innerWidth - 16;
      b.style.right = `${Math.round(innerWidth - edge + 6)}px`;
    }
    tellFrame();
  }
  // On phones the dashboard starts below the chat site's own top bar, so its sidebar button (and the swipe on it)
  // keeps working as on every other page. 0 = not a phone, or no such bar: start right below the SAP header.
  function navBottom() {
    if (!sidebarOverlay()) return 0;
    const t = document.getElementById("sidebar-toggle-button");
    if (!t || !t.offsetWidth) return 0;
    const nav = t.closest("nav");
    return Math.round((nav ? Math.min(nav.getBoundingClientRect().bottom, t.getBoundingClientRect().bottom + 8) : t.getBoundingClientRect().bottom + 8));
  }
  // Tell the dashboard how it is shown: beside the chat or not, a small screen, and whether the sidebar is hidden
  // (then the dashboard shows a ☰ button to open it).
  function tellFrame(force) {
    if (!frame) return;
    const msg = { sapb1: true, mode: split ? "split" : "full", narrow: innerWidth < SPLIT_MIN_WIDTH, menu: sidebarRight() === 0 && !navBottom() };
    const key = JSON.stringify(msg);
    if (key === told && !force) return;
    told = key;
    try { frame.contentWindow.postMessage(msg, location.origin); } catch (e) {}
  }
  function showDashboard() {
    if (!frame) {
      frame = document.createElement("iframe");
      frame.id = "sapb1-home";
      frame.title = "Home dashboard";
      frame.src = "/sapb1/home";
      // Below the SAP header bar (webui/fiori.css), beside the sidebar. The sidebar (z-index 50) slides over it on phones.
      frame.style.cssText = "position:fixed;top:var(--sap-shell-h,0px);border:0;height:calc(100dvh - var(--sap-shell-h,0px));z-index:39;" +
        "background:transparent;color-scheme:normal;box-shadow:1px 0 0 rgba(34,53,72,.15)";
      frame.addEventListener("load", () => tellFrame(true));
      document.body.append(frame);
    }
    split = false;  // "Dashboard" shows only the dashboard; only "Side by side" (or a question) puts it beside a chat
    frame.style.display = "block";
    openedAt = location.pathname;
    dismissed = false;
    sync();
  }
  function setSplit(on, hide) {
    split = on;
    if (hide && frame) frame.style.display = "none";
    sync();
  }
  function hideDashboard(startedChat = true) {
    if (startedChat) dismissed = true;
    split = false;
    if (frame) frame.style.display = "none";
    sync();
  }
  function closeOverlaySidebar() {
    if (sidebarOverlay()) document.querySelector('#sidebar button[aria-label="Close Sidebar"]')?.click();
  }
  // "Dashboard" in the left sidebar, styled like the sidebar's own items. The sidebar is redrawn by Open WebUI now and
  // then, so it is put back whenever it is missing.
  const ICON = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="size-4" aria-hidden="true">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z"/></svg>';
  function sidebarItems() {
    const items = [];
    // the open sidebar: the first row, above New Chat
    let item = document.getElementById("sapb1-dashboard-button");
    if (!item || !item.isConnected) {
      const before = document.querySelector("#sidebar a#sidebar-new-chat-button")?.parentElement;
      if (before) {
        const wrap = document.createElement("div");
        wrap.className = "px-1 flex justify-center text-gray-700 dark:text-gray-300";
        wrap.innerHTML = '<button id="sapb1-dashboard-button" type="button" aria-label="Dashboard" title="Show the dashboard" ' +
          'class="group grow flex items-center space-x-2 rounded-xl px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-900 transition outline-none">' +
          '<div class="self-center flex size-4 shrink-0 items-center justify-center">' + ICON + '</div>' +
          '<div class="flex flex-1 self-center translate-y-[0.5px]"><div class="self-center text-[0.8125rem] leading-5">Dashboard</div></div></button>';
        before.before(wrap);
        item = wrap.firstChild;
        item.addEventListener("click", () => { item.blur(); dashboardOnly(); closeOverlaySidebar(); });
      } else item = null;
    }
    if (item) items.push(item);
    // the closed sidebar on a PC: the first icon, above New Chat (the pencil; the logo at the top also says New Chat)
    let icon = document.getElementById("sapb1-dashboard-icon");
    if (!icon || !icon.isConnected) {
      const railSearch = [...document.querySelectorAll('#sidebar button[aria-label="Search"]:not([id])')].find((b) => b.offsetWidth > 0);
      const group = railSearch?.parentElement?.parentElement?.parentElement;  // New Chat, Search, Notes, each in its own box
      const railNew = group && [...group.children].find((c) => c.querySelector('a[aria-label="New Chat"]'));
      if (railNew) {
        const wrap = document.createElement("div");
        wrap.className = "flex";
        wrap.innerHTML = '<button id="sapb1-dashboard-icon" type="button" aria-label="Dashboard" title="Dashboard" draggable="false" ' +
          'class="cursor-pointer flex size-8 items-center justify-center transition group">' +
          '<div class="sapb1-dash-pill self-center flex size-[calc(30px*var(--app-text-scale,1))] items-center justify-center rounded-lg transition group-hover:bg-gray-100 dark:group-hover:bg-gray-900">' +
          ICON + '</div></button>';
        const box = document.createElement("div");
        box.append(wrap);
        railNew.before(box);
        icon = wrap.firstChild;
        // stop here: a click on the strip itself opens the sidebar
        icon.addEventListener("click", (e) => { e.stopPropagation(); icon.blur(); dashboardOnly(); });
      } else icon = null;
    }
    if (icon) items.push(icon);
    return items;
  }
  function sync() {
    const on = isOpen();
    for (const item of sidebarItems()) {
      const target = item.querySelector(".sapb1-dash-pill") || item;
      target.classList.toggle("bg-gray-100", on);
      target.classList.toggle("dark:bg-gray-900", on);
      item.setAttribute("aria-pressed", on ? "true" : "false");
    }
    place();
  }
  function typeIntoChat(text, send) {
    let tries = 0;
    const t = setInterval(() => {
      const box = document.getElementById("chat-input");
      if (!box && ++tries < 40) return;
      clearInterval(t);
      if (!box) return;
      box.focus();
      if (!text) return;
      if (box.tagName === "TEXTAREA") {
        box.value = text;
        box.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        document.execCommand("selectAll", false);
        document.execCommand("insertText", false, text);
      }
      if (send) setTimeout(() => document.getElementById("send-message-button")?.click(), 150);
    }, 100);
  }

  // Starting a chat from the left closes the dashboard; so does picking a chat from the list when the dashboard covers
  // the chat (phones), even the chat it was opened over.
  document.addEventListener("click", (e) => {
    if (!e.target.closest) return;
    if (e.target.closest('#sidebar-new-chat-button, #sidebar a[aria-label="New Chat"]')) hideDashboard();
    else if (e.target.closest('#sidebar a[href^="/c/"]') && isOpen() && !split) hideDashboard(false);
  }, true);
  // From the dashboard: ☰ (open the sidebar), "Start a new chat", "Close dashboard", and questions (the ask bar and
  // "Ask the AI" on each chart).
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data || !e.data.sapb1 || !e.data.action) return;
    if (e.data.action === "menu") {
      (document.getElementById("sidebar-toggle-button") || document.querySelector('button[aria-label="Open Sidebar"]'))?.click();
    }
    if (e.data.action === "chat") { hideDashboard(); typeIntoChat("", false); }
    if (e.data.action === "close") hideDashboard();
    // the Dashboard | Side by side | Chat switch at the top of the dashboard
    if (e.data.action === "view") {
      const v = e.data.view;
      if (v === "dash") dashboardOnly();
      if (v === "split" && innerWidth >= SPLIT_MIN_WIDTH) openBeside(e.data.chat);
      if (v === "chat") {
        if (home() && lastChat) { const c = lastChat; lastChat = ""; hideDashboard(true); go(c); }  // back to the chat you were in
        else { hideDashboard(home()); typeIntoChat("", false); }
      }
    }
    if (e.data.action === "ask" && typeof e.data.text === "string" && e.data.text.trim()) {
      const text = e.data.text.trim().slice(0, 2000);
      dismissed = true;
      if (innerWidth >= SPLIT_MIN_WIDTH) setSplit(true, false); else hideDashboard();
      typeIntoChat(text, true);
    }
  });
  // Side by side with a chosen chat: "new" (a new chat), or a chat id. The chat site's own page router follows any
  // link on the page, so a link is clicked instead of reloading the page.
  // Go to a page of the chat site without reloading: its page router follows any link that is clicked.
  function go(href) {
    if (location.pathname === href) return;
    const a = document.createElement("a");
    a.href = href; a.style.display = "none";
    document.body.append(a); a.click(); a.remove();
  }
  // "Dashboard": only the dashboard. A chat that was open is left (so it is no longer marked in the sidebar);
  // "Chat" goes back to it.
  let lastChat = "";
  function dashboardOnly() {
    if (location.pathname.startsWith("/c/")) { lastChat = location.pathname; go("/"); }
    showDashboard();
  }
  function openBeside(chat) {
    split = true;
    const href = chat && chat !== "new" ? `/c/${encodeURIComponent(chat)}` : "/";
    openedAt = href;
    sync();
    lastChat = "";
    go(href);
    if (href === "/") typeIntoChat("", false);
  }
  // Signing in passes through /auth first, so keep watching for the start screen instead of checking once.
  // Opening another chat hides a full-screen dashboard; a dashboard beside the chat stays.
  setInterval(() => {
    if (!chatPage()) { if (isOpen()) hideDashboard(false); }
    else if (!home() && isOpen() && !split && location.pathname !== openedAt) hideDashboard(false);
    else if (home() && !isOpen() && !dismissed && document.body) showDashboard();
    else sync();
  }, 300);
  addEventListener("resize", place);
})();
