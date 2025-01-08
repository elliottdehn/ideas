/*******************************************************
 * server.js
 *
 * Supports step types:
 *   1) "snippet"
 *   2) "http"
 *   3) "filter"
 *
 * If a filter step returns false, we stop execution.
 *
 * /runSequence:
 *   Body: { variables: [...], steps: [...] }
 *   - Expands variables (with placeholders, cycle detection)
 *   - Runs steps in order
 *   - Returns JSON with { steps: [...], vars: {...} }
 *******************************************************/

const express = require('express');
const path = require('path');
const bodyParser = require('body-parser');
const { VM } = require('vm2');

// If Node <18, do:
// const fetch = require('node-fetch');

const app = express();
app.use(bodyParser.json());

/*******************************************************
 * Serve index.html (adapt the path as needed).
 * This is optional if you're hosting the front-end from here.
 *******************************************************/
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

/*******************************************************
 * Placeholder expansion & variable expansion utilities
 *******************************************************/

/** Find all {{varName}} placeholders in a string */
function findAllPlaceholders(str) {
  if (typeof str !== 'string') return [];
  const placeholders = [];
  let startIndex = 0;
  while (true) {
    const openIdx = str.indexOf('{{', startIndex);
    if (openIdx === -1) break;
    const closeIdx = str.indexOf('}}', openIdx + 2);
    if (closeIdx === -1) break;

    const varName = str.slice(openIdx + 2, closeIdx).trim();
    placeholders.push(varName);
    startIndex = closeIdx + 2;
  }
  return placeholders;
}

/** Expand all {{varName}} placeholders in 'str' using finalVars lookup */
function expandPlaceholders(str, lookup) {
  if (typeof str !== 'string') return str;
  let result = '';
  let i = 0;
  while (i < str.length) {
    const openIdx = str.indexOf('{{', i);
    if (openIdx === -1) {
      result += str.slice(i);
      break;
    }
    result += str.slice(i, openIdx);

    const closeIdx = str.indexOf('}}', openIdx + 2);
    if (closeIdx === -1) {
      // no matching
      result += str.slice(openIdx);
      break;
    }

    const varName = str.slice(openIdx + 2, closeIdx).trim();
    if (lookup.hasOwnProperty(varName)) {
      result += JSON.stringify(lookup[varName]);
    } else {
      // not found => null
      result += 'null';
    }
    i = closeIdx + 2;
  }
  return result;
}

/** Build a quick lookup of already expanded variables */
function buildExpandedLookup(allVarsMap) {
  const ret = {};
  for (const k of Object.keys(allVarsMap)) {
    if (allVarsMap[k].__expanded !== undefined) {
      ret[k] = allVarsMap[k].__expanded;
    }
  }
  return ret;
}

/**
 * Recursively expand one variable's raw string => final typed data
 *  - cycle detection
 *  - repeated pass until stable
 */
function expandVariable(varName, allVarsMap, inProgress) {
  // memoized?
  if (allVarsMap[varName].__expanded !== undefined) {
    return allVarsMap[varName].__expanded;
  }

  if (inProgress.has(varName)) {
    throw new Error(`Cycle detected: ${varName}`);
  }
  inProgress.add(varName);

  let expandedStr = allVarsMap[varName].__raw;
  let changed = true;
  while (changed) {
    changed = false;
    const placeholders = findAllPlaceholders(expandedStr);

    // ensure placeholders are expanded
    for (const ph of placeholders) {
      if (allVarsMap[ph] && allVarsMap[ph].__expanded === undefined) {
        expandVariable(ph, allVarsMap, inProgress);
      }
    }

    const oldStr = expandedStr;
    const lookupMap = buildExpandedLookup(allVarsMap);
    expandedStr = expandPlaceholders(expandedStr, lookupMap);
    if (expandedStr !== oldStr) changed = true;
  }

  // parse or keep as string
  let finalVal;
  try {
    finalVal = JSON.parse(expandedStr);
  } catch {
    finalVal = expandedStr;
  }
  allVarsMap[varName].__expanded = finalVal;

  inProgress.delete(varName);
  return finalVal;
}

/** Expand & parse a literal field with placeholders */
function expandAndParse(raw, finalVars) {
  const expanded = expandPlaceholders(raw || '', finalVars);
  try {
    return JSON.parse(expanded);
  } catch {
    return expanded;
  }
}

/*******************************************************
 * runAllSteps(variables, steps)
 * - Expands variables
 * - Executes steps in order
 * - If filter step returns false => stop
 *******************************************************/
async function runAllSteps(variables, steps) {
  // 1) Build map of varName => { __raw, __expanded }
  const allVarsMap = {};
  for (const v of variables) {
    if (!v.varName) continue;
    allVarsMap[v.varName] = {
      __raw: v.literalValue,
      __expanded: undefined
    };
  }

  // 2) Expand each variable
  const inProgress = new Set();
  for (const v of variables) {
    if (!v.varName) continue;
    expandVariable(v.varName, allVarsMap, inProgress);
  }

  // finalVars => { varName: expandedValue }
  const finalVars = {};
  for (const k of Object.keys(allVarsMap)) {
    finalVars[k] = allVarsMap[k].__expanded;
  }

  // 3) Steps
  const results = [];
  let stopped = false;

  for (let i = 0; i < steps.length; i++) {
    if (stopped) break;

    const step = steps[i];
    let stepResult = {
      stepIndex: i,
      type: step.type,
      error: null,
      result: null
    };

    if (step.type === 'snippet') {
      // snippet
      const snippetRaw = expandPlaceholders(step.snippet || '', finalVars);
      let inputVal;
      if (step.inputMode === 'var') {
        inputVal = finalVars.hasOwnProperty(step.inputValue)
          ? finalVars[step.inputValue]
          : null;
      } else {
        inputVal = expandAndParse(step.inputValue, finalVars);
      }

      const codeToRun = `
        const transformFn = (${snippetRaw});
        transformFn(${JSON.stringify(inputVal)});
      `;

      try {
        const vm = new VM({
          timeout: 1000,
          eval: false,
          wasm: false,
          allowAsync: false,
          fixAsync: false,
          require: false,
          sandbox: {}
        });
        const snippetResult = vm.run(codeToRun);
        stepResult.snippetAfterEmbed = snippetRaw;
        stepResult.result = snippetResult;
        if (step.storeVar) {
          finalVars[step.storeVar] = snippetResult;
        }
      } catch (err) {
        stepResult.error = `Snippet step error: ${err.message}`;
      }

    } else if (step.type === 'http') {
      // http
      const method = expandPlaceholders(step.method || 'GET', finalVars);
      const url = expandPlaceholders(step.url || '', finalVars);

      const headersVal = expandAndParse(step.headers, finalVars) || {};
      const cookiesVal = expandAndParse(step.cookies, finalVars);
      const bodyVal = expandPlaceholders(step.body || '', finalVars);

      // if cookiesVal => set 'Cookie' header
      if (cookiesVal && typeof cookiesVal === 'string') {
        if (!headersVal['Cookie']) {
          headersVal['Cookie'] = cookiesVal;
        } else {
          headersVal['Cookie'] += '; ' + cookiesVal;
        }
      }

      let httpResult = null;
      try {
        const fetchOptions = { method, headers: headersVal };
        if (bodyVal) {
          fetchOptions.body = bodyVal;
        }
        const resp = await fetch(url, fetchOptions);
        const text = await resp.text();
        const minimalResp = {
          status: resp.status,
          headers: {},
          body: text
        };
        for (const [hk, hv] of resp.headers.entries()) {
          minimalResp.headers[hk] = hv;
        }
        httpResult = minimalResp;
        stepResult.result = httpResult;
        if (step.storeVar) {
          finalVars[step.storeVar] = httpResult;
        }
      } catch (err) {
        stepResult.error = `HTTP step error: ${err.message}`;
      }

    } else if (step.type === 'filter') {
      // filter is like snippet but must return boolean
      const filterRaw = expandPlaceholders(step.filterCode || '', finalVars);
      let inputVal;
      if (step.inputMode === 'var') {
        inputVal = finalVars.hasOwnProperty(step.inputValue)
          ? finalVars[step.inputValue]
          : null;
      } else {
        inputVal = expandAndParse(step.inputValue, finalVars);
      }

      const codeToRun = `
        const filterFn = (${filterRaw});
        filterFn(${JSON.stringify(inputVal)});
      `;

      try {
        const vm = new VM({
          timeout: 1000,
          eval: false,
          wasm: false,
          allowAsync: false,
          fixAsync: false,
          require: false,
          sandbox: {}
        });
        const filterResult = vm.run(codeToRun);
        stepResult.snippetAfterEmbed = filterRaw;
        stepResult.result = filterResult;
        if (typeof filterResult !== 'boolean') {
          stepResult.error = `Filter step returned non-boolean: ${JSON.stringify(filterResult)}`;
        } else if (!filterResult) {
          // stop the sequence
          stepResult.error = `Filter returned false. Stopping sequence at step #${i}.`;
          stopped = true;
        }
      } catch (err) {
        stepResult.error = `Filter step error: ${err.message}`;
      }

    } else {
      stepResult.error = `Unknown step type: ${step.type}`;
    }

    results.push(stepResult);
  }

  return {
    steps: results,
    vars: finalVars
  };
}

/*******************************************************
 * POST /runSequence
 *******************************************************/
app.post('/runSequence', async (req, res) => {
  const { variables, steps } = req.body;
  if (!Array.isArray(variables) || !Array.isArray(steps)) {
    return res.json({ error: 'Invalid request. Must include "variables" and "steps" arrays.' });
  }
  try {
    const result = await runAllSteps(variables, steps);
    res.json(result);
  } catch (err) {
    res.json({ error: err.message });
  }
});

/*******************************************************
 * Start the server
 *******************************************************/
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running at http://localhost:${PORT}`);
});
