// Small, dependency-free helpers shared by every page. Server data is inserted with
// textContent (or escaped first), never raw innerHTML.
"use strict";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function requestJson(method, url, payload) {
  const init = { method, headers: { Accept: "application/json" } };
  if (payload !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload);
  }
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    throw new Error((body.detail || "Session expired.") + " Reload the page to log in.");
  }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

const postJson = (url, payload) => requestJson("POST", url, payload);
const getJson = (url) => requestJson("GET", url);

window.HackNC = { el, getJson, postJson };
