const fs = require("fs");
const vm = require("vm");

class Element {
  constructor({ name = "", hidden = false } = {}) {
    this.name = name;
    this.hidden = hidden;
    this.listeners = {};
    this.children = new Map();
    this.childLists = new Map();
  }
  querySelector(selector) { return this.children.get(selector) || null; }
  querySelectorAll(selector) { return this.childLists.get(selector) || []; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach((listener) => listener(event)); }
  contains() { return false; }
  focus() { this.focused = true; }
}

const regulators = new Element();
const jurisdictions = new Element();
const form = new Element();
form.submits = 0;
form.requestSubmit = () => { form.submits += 1; };
form.childLists.set('input[type="date"], input[type="checkbox"]:not(.reg-select-list input)', []);

function setupSelect(select, checkboxName, searchable) {
  const toggle = new Element();
  const panel = new Element({ hidden: true });
  const checkbox = new Element({ name: checkboxName });
  select.children.set(".reg-select-toggle", toggle);
  select.children.set(".reg-select-panel", panel);
  select.children.set('.reg-select-list input[type="checkbox"]', checkbox);
  select.childLists.set(".reg-select-list label.reg", []);
  select.childLists.set('.reg-select-list input[type="checkbox"]', [checkbox]);
  if (searchable) select.children.set(".reg-filter", new Element());
  return { toggle, panel, checkbox };
}

const regulator = setupSelect(regulators, "regulator_id", true);
const jurisdiction = setupSelect(jurisdictions, "jurisdiction", false);
const documentListeners = {};
global.document = {
  addEventListener(type, listener) { (documentListeners[type] ||= []).push(listener); },
  getElementById() { return null; },
  querySelector(selector) { return selector === "form.filters" ? form : null; },
  querySelectorAll(selector) { return selector === ".reg-select" ? [regulators, jurisdictions] : []; },
};
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });

vm.runInThisContext(fs.readFileSync("webui/static/app.js", "utf8"));
documentListeners.DOMContentLoaded.forEach((listener) => listener());

for (const { toggle, panel, checkbox } of [regulator, jurisdiction]) {
  toggle.dispatch("click", { stopPropagation: () => {} });
  if (panel.hidden) throw new Error("dropdown did not open");
  checkbox.dispatch("change");
}
if (form.submits !== 2) throw new Error(`expected two checkbox submissions, got ${form.submits}`);
console.log("both dropdowns open and both checkbox groups auto-submit");
