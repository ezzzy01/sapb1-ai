// SAP B1 AI: Company dropdown on the login page. Open WebUI loads /static/loader.js on every page.
// The company list (names and menu ids) is written by ./company.sh to webui/companies.json. After sign-in the chosen
// company is opened (/?model=<id>); Open WebUI only opens it if the person's group may use it.
(() => {
  const GO = "sapb1.company.go"; // this tab: company to open after sign-in
  const LAST = "sapb1.company.last"; // this browser: last company chosen, preselected next time
  const store = (s, k, v) => { try { v === undefined ? s.removeItem(k) : s.setItem(k, v); } catch (e) { /* storage blocked */ } };
  const read = (s, k) => { try { return s.getItem(k); } catch (e) { return null; } };

  let companies = null;
  fetch("/static/sapb1/companies.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : []))
    .then((list) => { companies = Array.isArray(list) ? list : []; tick(); })
    .catch(() => { companies = []; });

  function wanted() {
    const redirect = new URLSearchParams(location.search).get("redirect") || "";
    const fromLink = new URLSearchParams(redirect.split("?")[1] || "").get("model");
    const ids = companies.map((c) => c.model);
    return [fromLink, read(localStorage, LAST)].find((id) => id && ids.includes(id)) || companies[0].model;
  }

  function addDropdown() {
    const email = document.querySelector("form #email");
    if (!email || document.getElementById("sapb1-company")) return;
    const block = document.createElement("div");
    block.className = "mb-2";
    block.innerHTML =
      '<label for="sapb1-company" class="text-[0.8125rem] leading-5 font-normal text-left mb-1 block">Company</label>' +
      '<select id="sapb1-company" class="my-0.5 w-full text-[0.8125rem] leading-5 outline-hidden bg-transparent"></select>';
    const select = block.querySelector("select");
    for (const c of companies) select.add(new Option(c.name, c.model));
    select.value = wanted();
    const remember = () => { store(sessionStorage, GO, select.value); store(localStorage, LAST, select.value); };
    select.addEventListener("change", remember);
    email.closest("form").addEventListener("submit", remember, true);
    remember();
    email.closest(".mb-2").before(block);
  }

  function openChosen() {
    const id = read(sessionStorage, GO);
    if (!id || location.pathname.startsWith("/auth")) return;
    store(sessionStorage, GO); // once per sign-in
    if (new URLSearchParams(location.search).get("model") !== id) location.replace("/?model=" + encodeURIComponent(id));
  }

  function tick() {
    if (!companies || !companies.length) return;
    if (location.pathname.startsWith("/auth")) addDropdown();
    else openChosen();
  }

  // The app swaps pages without reloading, so keep checking: re-add the dropdown if the form is redrawn (e.g. after
  // switching between sign-in and sign-up) and notice the move away from the login page after signing in.
  setInterval(tick, 300);
})();
