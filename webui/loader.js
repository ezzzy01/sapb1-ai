// SAP B1 AI: people sign in on the shared sign-in page (/portal/login, src/sapb1_ai/portal.py), which sends them to
// the company they chose. Each company has its own Open WebUI, so this page only ever shows that company. Open WebUI
// loads /static/loader.js on every page; this script:
//  - sends Open WebUI's own sign-in page back to the shared one once the shared sign-in has ended (Open WebUI signs
//    people in by itself while it is valid);
//  - keeps the model menu fixed (each site has just its own company).
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
})();
