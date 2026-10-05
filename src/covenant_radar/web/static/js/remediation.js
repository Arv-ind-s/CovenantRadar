/*
 * Remediation planner: keep each lever's read-out in step with its slider
 * while it moves. The server re-runs the simulation (htmx, debounced) and
 * is the only source of the figures; this only formats the size label.
 */
(function () {
  "use strict";

  function format(value, unit) {
    var number = Number(value);
    if (!isFinite(number)) return "";
    if (unit === "crore") return "₹" + Math.round(number).toLocaleString("en-US") + " cr";
    if (unit === "bp") return Math.round(number) + " bp";
    if (unit === "percent") return number.toFixed(1) + "%";
    if (unit === "share") return Math.round(number) + "% of the trend";
    return number.toFixed(2) + "x";
  }

  function update(input) {
    var output = document.getElementById(input.id + "-value");
    if (output) output.textContent = format(input.value, input.getAttribute("data-unit"));
  }

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (target && target.matches && target.matches(".remedy-lever input[type=range]")) update(target);
  });
})();
