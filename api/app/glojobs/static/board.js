// Company chips and the search box on a board page (the reference site's inline script, moved
// to a file so the CSP can stay `script-src 'self'`). Does nothing on pages without roles.
(() => {
  const q = document.getElementById("q"), status = document.getElementById("status");
  if (!q || !status) return;
  const TOTAL = Number(status.dataset.total || 0);
  const empty = document.getElementById("empty"), emptyQ = document.getElementById("empty-q");
  const picks = [...document.querySelectorAll(".pick")], rows = [...document.querySelectorAll(".role")], cos = [...document.querySelectorAll(".co")];
  let company = new URLSearchParams(location.search).get("company") || "all";
  if (!picks.some((p) => p.dataset.filter === company)) company = "all";
  function run() {
    const terms = q.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    let n = 0;
    rows.forEach((r) => {
      const ok = (company === "all" || r.dataset.company === company) && terms.every((t) => r.dataset.search.includes(t));
      r.hidden = !ok; if (ok) n++;
    });
    cos.forEach((c) => { c.hidden = !c.querySelector(".role:not([hidden])"); });
    picks.forEach((p) => p.setAttribute("aria-pressed", String(p.dataset.filter === company)));
    status.textContent = n === TOTAL ? "Showing all " + TOTAL + " roles" : "Showing " + n + " of " + TOTAL + " roles";
    if (empty) empty.hidden = n > 0;
    if (emptyQ) emptyQ.textContent = q.value.trim() ? "“" + q.value.trim() + "”" : "this filter";
    const u = new URL(location.href);
    company === "all" ? u.searchParams.delete("company") : u.searchParams.set("company", company);
    history.replaceState(null, "", u);
  }
  picks.forEach((p) => p.addEventListener("click", () => { company = p.dataset.filter; run(); }));
  q.addEventListener("input", run);
  const clear = document.getElementById("clear");
  if (clear) clear.addEventListener("click", () => { q.value = ""; company = "all"; run(); q.focus(); });
  run();
})();
