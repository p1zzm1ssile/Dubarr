"use strict";

const form = document.getElementById("login-form");
const errorBox = document.getElementById("form-error");
const mode = form.dataset.mode;

const RULES = [
  ["At least 8 characters", (p) => p.length >= 8],
  ["An uppercase letter", (p) => /[A-Z]/.test(p)],
  ["A lowercase letter", (p) => /[a-z]/.test(p)],
  ["A number", (p) => /[0-9]/.test(p)],
  ["A special character, like ! ? # or @", (p) => /[^A-Za-z0-9]/.test(p)],
];

function renderRules() {
  const list = document.getElementById("rules");
  if (!list) return;
  const password = form.password.value;
  list.replaceChildren(...RULES.map(([text, check]) => {
    const li = document.createElement("li");
    li.textContent = text;
    if (check(password)) li.classList.add("met");
    return li;
  }));
}

if (mode === "setup") {
  form.password.addEventListener("input", renderRules);
  renderRules();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  const username = form.username.value.trim();
  const password = form.password.value;

  if (mode === "setup" && !form.code.value.trim()) { errorBox.textContent = "Enter the setup code from Dubarr's log"; return; }
  if (!username) { errorBox.textContent = "Enter a username"; return; }
  if (mode === "setup") {
    const missing = RULES.filter(([, check]) => !check(password));
    if (missing.length) { errorBox.textContent = "Your password still needs: " + missing.map(([t]) => t.toLowerCase()).join(", "); return; }
    if (password !== form.confirm.value) { errorBox.textContent = "The passwords don't match"; return; }
  }

  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const response = await fetch(mode === "setup" ? "/api/setup" : "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dubarr": "1" },
      body: JSON.stringify(mode === "setup" ? { code: form.code.value.trim(), username, password } : { username, password }),
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) { window.location.href = "/"; return; }
    errorBox.textContent = data.error || "Something went wrong";
  } catch {
    errorBox.textContent = "Can't reach Dubarr";
  } finally {
    button.disabled = false;
  }
});
