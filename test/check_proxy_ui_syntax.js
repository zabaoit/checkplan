const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const files = [
  path.join(root, "web", "static", "app.js"),
  path.join(root, "web", "static", "session.js"),
  path.join(root, "web", "static", "upi.js"),
  path.join(root, "web", "static", "link.js"),
  path.join(root, "web", "static", "settings_panel.js"),
];

for (const file of files) {
  const src = fs.readFileSync(file, "utf8");
  new Function(src);
}

const appSrc = fs.readFileSync(path.join(root, "web", "static", "app.js"), "utf8");
if (!appSrc.includes("function playSuccessAlert()")) {
  throw new Error("playSuccessAlert missing from app.js");
}
if (!appSrc.includes("playErrorAlert, playSuccessAlert")) {
  throw new Error("playSuccessAlert is not exposed on GptUi");
}

for (const name of ["app.js", "session.js", "upi.js", "link.js"]) {
  const src = fs.readFileSync(path.join(root, "web", "static", name), "utf8");
  if (!src.includes("playSuccessAlert")) {
    throw new Error(`success alert trigger missing from ${name}`);
  }
}

const settingsPanelSrc = fs.readFileSync(path.join(root, "web", "static", "settings_panel.js"), "utf8");
const indexSrc = fs.readFileSync(path.join(root, "web", "static", "index.html"), "utf8");
if (!indexSrc.includes("proxy-show-raw-logs")) {
  throw new Error("raw proxy log checkbox missing from index.html");
}
if (!settingsPanelSrc.includes("proxy.show_raw_in_logs")) {
  throw new Error("raw proxy log setting wiring missing from settings_panel.js");
}
if (!indexSrc.includes("proxy-pool-export")) {
  throw new Error("proxy export button missing from index.html");
}
if (!settingsPanelSrc.includes("function exportProxiesTxt()")) {
  throw new Error("proxy export function missing from settings_panel.js");
}
if (!indexSrc.includes("proxy-pool-clear-all")) {
  throw new Error("proxy clear-all button missing from index.html");
}
if (!settingsPanelSrc.includes("function clearAllAndSave()")) {
  throw new Error("proxy clear-all function missing from settings_panel.js");
}
if (!indexSrc.includes("proxy-pool-clear-dead")) {
  throw new Error("proxy clear-dead button missing from index.html");
}
if (!settingsPanelSrc.includes("function clearDeadAndSave()")) {
  throw new Error("proxy clear-dead function missing from settings_panel.js");
}
for (const marker of [
  "proxies: proxies",
  "rotation_mode: dom.modeSelect.value",
]) {
  if (!settingsPanelSrc.includes(marker)) {
    throw new Error(`proxy save endpoint payload marker missing from settings_panel.js: ${marker}`);
  }
}
if (!settingsPanelSrc.includes('api("/api/proxy/pool"')) {
  throw new Error("proxy save endpoint payload missing from settings_panel.js");
}

console.log("OK UI JS syntax and success alert wiring");
