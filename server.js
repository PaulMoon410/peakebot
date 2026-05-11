const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const { spawn } = require("child_process");

const PORT = process.env.PORT || 3000;
const PYTHON_PORT = parseInt(process.env.PYTHON_PORT || "5001", 10);
const PYTHON_HOST = process.env.PYTHON_HOST || "127.0.0.1";

function normalizePythonServerUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    // Render can resolve localhost to IPv6 first; force IPv4 loopback for Python sidecar.
    if (parsed.hostname === "localhost") {
      parsed.hostname = "127.0.0.1";
    }
    return parsed.origin;
  } catch {
    return `http://${PYTHON_HOST}:${PYTHON_PORT}`;
  }
}

const PYTHON_KNOWLEDGE_SERVER = normalizePythonServerUrl(
  process.env.PYTHON_KNOWLEDGE_SERVER || `http://${PYTHON_HOST}:${PYTHON_PORT}`,
);
const PYTHON_CHAT_TIMEOUT_MS = parseInt(process.env.PYTHON_CHAT_TIMEOUT_MS || "90000", 10);
const START_PYTHON_SERVER = process.env.START_PYTHON_SERVER !== "false";
const FTP_BRAIN_DIR = process.env.FTP_BRAIN_DIR || "/ai/brain";

const DATA_DIR = path.join(__dirname, "data");
const MEMORY_FILE = path.join(DATA_DIR, "memory.json");
const KNOWLEDGE_DIR = path.join(DATA_DIR, "knowledge");

function ensureDirectories() {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  }
  if (!fs.existsSync(KNOWLEDGE_DIR)) {
    fs.mkdirSync(KNOWLEDGE_DIR, { recursive: true });
  }
}

function defaultMemory() {
  const now = new Date().toISOString();
  return {
    profile: {
      siteOrigin: "server",
      createdAt: now,
      updatedAt: now,
    },
    facts: [],
    notes: [],
    conversations: [],
    remoteSources: [],
  };
}

function readMemory() {
  ensureDirectories();
  try {
    const raw = fs.readFileSync(MEMORY_FILE, "utf8");
    const parsed = JSON.parse(raw);
    return { ...defaultMemory(), ...parsed };
  } catch {
    return defaultMemory();
  }
}

function isValidMemory(value) {
  return value && typeof value === "object";
}

function writeMemory(memory) {
  ensureDirectories();
  const merged = { ...defaultMemory(), ...memory };
  merged.profile.updatedAt = new Date().toISOString();
  fs.writeFileSync(MEMORY_FILE, JSON.stringify(merged, null, 2), "utf8");
  return merged;
}

function saveKnowledge(conversation) {
  ensureDirectories();
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const filename = `conversation-${timestamp}.json`;
  const filepath = path.join(KNOWLEDGE_DIR, filename);
  
  fs.writeFileSync(filepath, JSON.stringify({
    timestamp: new Date().toISOString(),
    user_message: conversation.user,
    ai_response: conversation.ai,
    memory_state: conversation.memory,
  }, null, 2), "utf8");
  
  return filepath;
}

// ---------------------------------------------------------------------------
// Python knowledge server bridge
// ---------------------------------------------------------------------------

/**
 * GET knowledge entries from the Python server.
 * Returns an array, or null if the Python server is unavailable.
 */
async function pyGetKnowledge(limit = 50) {
  return new Promise((resolve) => {
    const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/knowledge?limit=${limit}`);
    const transport = pyUrl.protocol === "https:" ? https : http;

    const options = {
      hostname: pyUrl.hostname,
      port: pyUrl.port || 5001,
      path: pyUrl.pathname + pyUrl.search,
      method: "GET",
      timeout: 8000,
    };

    const req = transport.request(options, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          resolve(body.files || []);
        } catch (_) {
          resolve(null);
        }
      });
    });

    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.on("error", () => resolve(null));
    req.end();
  });
}

/**
 * Search knowledge entries via Python server.
 * Returns { results, count } or null if unavailable.
 */
async function pySearchKnowledge(query, limit = 8) {
  return new Promise((resolve) => {
    const safeQuery = encodeURIComponent(String(query || "").trim());
    const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/knowledge/search?q=${safeQuery}&limit=${limit}`);
    const transport = pyUrl.protocol === "https:" ? https : http;

    const options = {
      hostname: pyUrl.hostname,
      port: pyUrl.port || 5001,
      path: pyUrl.pathname + pyUrl.search,
      method: "GET",
      timeout: 12000,
    };

    const req = transport.request(options, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          resolve({ results: body.results || [], count: body.count || 0 });
        } catch (_) {
          resolve(null);
        }
      });
    });

    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.on("error", () => resolve(null));
    req.end();
  });
}

/**
 * Check if the Python knowledge server is reachable.
 */
async function pyHealthCheck() {
  return new Promise((resolve) => {
    const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/health`);
    const transport = pyUrl.protocol === "https:" ? https : http;
    const req = transport.request(
      { hostname: pyUrl.hostname, port: pyUrl.port || 5001, path: "/health", method: "GET", timeout: 3000 },
      (res) => { resolve(res.statusCode === 200); },
    );
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
    req.end();
  });
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { 
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  });
  res.end(JSON.stringify(payload));
}

function getContentType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const map = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
  };
  return map[ext] || "application/octet-stream";
}

function serveStatic(req, res, pathname) {
  const safePath = pathname === "/" ? "/index.html" : pathname;
  const normalized = path.normalize(safePath).replace(/^([.][.][/\\])+/, "");
  const filePath = path.join(__dirname, normalized);

  if (!filePath.startsWith(__dirname)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  fs.readFile(filePath, (error, data) => {
    if (error) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }

    res.writeHead(200, { "Content-Type": getContentType(filePath) });
    res.end(data);
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);

  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(200, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    });
    res.end();
    return;
  }

  if (url.pathname === "/api/health" && req.method === "GET") {
    const pyOk = await pyHealthCheck();
    sendJson(res, 200, {
      ok: true,
      service: "peakebot",
      mode: "python-memory-engine",
      pythonKnowledgeServer: PYTHON_KNOWLEDGE_SERVER,
      pythonKnowledgeServerOnline: pyOk,
      timestamp: new Date().toISOString(),
    });
    return;
  }

  if (url.pathname === "/api/chat" && req.method === "POST") {
    const chunks = [];
    let receivedBytes = 0;
    const maxBytes = 10 * 1024 * 1024;

    req.on("data", (chunk) => {
      receivedBytes += chunk.length;
      if (receivedBytes > maxBytes) {
        sendJson(res, 413, { error: "Payload too large." });
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", async () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        const payload = JSON.parse(raw || "{}");
        
        if (!payload.prompt) {
          sendJson(res, 400, { error: "Missing prompt field." });
          return;
        }

        const text = payload.prompt.trim();
        const memory = payload.memory || {};

        try {
          const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/chat`);
          const pyTransport = pyUrl.protocol === "https:" ? https : http;
          const pyPayload = JSON.stringify({ prompt: text, memory });

          const pyResponse = await new Promise((resolve, reject) => {
            const options = {
              hostname: pyUrl.hostname,
              port: pyUrl.port || 5001,
              path: pyUrl.pathname,
              method: "POST",
              timeout: PYTHON_CHAT_TIMEOUT_MS,
              headers: {
                "Content-Type": "application/json",
                "Content-Length": Buffer.byteLength(pyPayload),
              },
            };

            const pyReq = pyTransport.request(options, (pyRes) => {
              const pyChunks = [];
              pyRes.on("data", (c) => pyChunks.push(c));
              pyRes.on("end", () => {
                const body = Buffer.concat(pyChunks).toString("utf8");
                try {
                  resolve({
                    status: pyRes.statusCode,
                    data: JSON.parse(body),
                  });
                } catch {
                  reject(new Error(`Invalid JSON from Python: ${body.slice(0, 140)}`));
                }
              });
            });

            pyReq.on("timeout", () => {
              pyReq.destroy();
              reject(new Error("Python server timeout"));
            });
            pyReq.on("error", reject);
            pyReq.write(pyPayload);
            pyReq.end();
          });

          if (pyResponse.status !== 200) {
            throw new Error(`Python returned ${pyResponse.status}: ${pyResponse.data.error || "unknown error"}`);
          }

          if (!pyResponse.data.response) {
            throw new Error("No response field from Python");
          }

          sendJson(res, 200, {
            response: pyResponse.data.response,
            source: "python-memory-engine",
            relevantCount: pyResponse.data.relevant_count || 0,
            verification: pyResponse.data.verification || null,
            ftp: {
              ok: pyResponse.data.ftp_saved === true,
              skipped: false,
              dailyFile: pyResponse.data.daily_file || null,
              error: pyResponse.data.ftp_error || null,
            },
          });
        } catch (pythonError) {
          console.error("[main] Python chat failed:", pythonError.message);
          sendJson(res, 502, {
            error: `AI unavailable: Python memory engine failed (${pythonError.message})`,
          });
        }
      } catch (error) {
        sendJson(res, 400, { error: `Invalid JSON: ${error.message}` });
      }
    });

    req.on("error", () => {
      sendJson(res, 400, { error: "Request stream error." });
    });
    return;
  }

  if (url.pathname === "/api/memory" && req.method === "GET") {
    const memory = readMemory();
    sendJson(res, 200, memory);
    return;
  }

  if (url.pathname === "/api/memory" && req.method === "PUT") {
    const chunks = [];
    let receivedBytes = 0;
    const maxBytes = 2 * 1024 * 1024;

    req.on("data", (chunk) => {
      receivedBytes += chunk.length;
      if (receivedBytes > maxBytes) {
        sendJson(res, 413, { error: "Payload too large." });
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        const parsed = JSON.parse(raw || "{}");
        if (!isValidMemory(parsed)) {
          sendJson(res, 400, { error: "Invalid memory JSON payload." });
          return;
        }

        const saved = writeMemory(parsed);
        sendJson(res, 200, { ok: true, updatedAt: saved.profile.updatedAt });
      } catch {
        sendJson(res, 400, { error: "Invalid JSON body." });
      }
    });

    req.on("error", () => {
      sendJson(res, 400, { error: "Request stream error." });
    });
    return;
  }

  if (url.pathname === "/api/knowledge" && req.method === "GET") {
    try {
      ensureDirectories();
      const files = fs.readdirSync(KNOWLEDGE_DIR)
        .filter((f) => f.endsWith(".json"))
        .sort()
        .reverse()
        .slice(0, 50);

      const conversations = files.map((file) => {
        const filepath = path.join(KNOWLEDGE_DIR, file);
        const content = fs.readFileSync(filepath, "utf8");
        return JSON.parse(content);
      });

      sendJson(res, 200, { conversations });
    } catch (error) {
      sendJson(res, 500, { error: `Failed to read knowledge: ${error.message}` });
    }
    return;
  }

  if (url.pathname === "/api/knowledge" && req.method === "POST") {
    const chunks = [];
    let receivedBytes = 0;
    const maxBytes = 5 * 1024 * 1024;

    req.on("data", (chunk) => {
      receivedBytes += chunk.length;
      if (receivedBytes > maxBytes) {
        sendJson(res, 413, { error: "Payload too large." });
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", async () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        const payload = JSON.parse(raw || "{}");

        if (!payload.user_message || !payload.ai_response) {
          sendJson(res, 400, { error: "Missing user_message or ai_response." });
          return;
        }

        // Proxy to Python /knowledge POST
        const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/knowledge`);
        const pyTransport = pyUrl.protocol === "https:" ? https : http;
        const pyPayload = JSON.stringify(payload);

        const pyResponse = await new Promise((resolve, reject) => {
          const options = {
            hostname: pyUrl.hostname,
            port: pyUrl.port || 5001,
            path: pyUrl.pathname,
            method: "POST",
              timeout: PYTHON_CHAT_TIMEOUT_MS,
            headers: {
              "Content-Type": "application/json",
              "Content-Length": Buffer.byteLength(pyPayload),
            },
          };

          const pyReq = pyTransport.request(options, (pyRes) => {
            const pyChunks = [];
            pyRes.on("data", (c) => pyChunks.push(c));
            pyRes.on("end", () => {
              const body = Buffer.concat(pyChunks).toString("utf8");
              try {
                resolve({
                  status: pyRes.statusCode,
                  data: JSON.parse(body),
                });
              } catch {
                reject(new Error(`Invalid JSON from Python: ${body.slice(0, 100)}`));
              }
            });
          });

          pyReq.on("timeout", () => {
            pyReq.destroy();
            reject(new Error("Python server timeout"));
          });
          pyReq.on("error", reject);
          pyReq.write(pyPayload);
          pyReq.end();
        });

        if (pyResponse.status !== 201 && pyResponse.status !== 200) {
          throw new Error(`Python returned ${pyResponse.status}: ${pyResponse.data.error || "unknown error"}`);
        }

        console.log(`[main] Admin knowledge learned from frontend`);
        sendJson(res, 200, pyResponse.data);
      } catch (error) {
        console.error("[main] Knowledge POST failed:", error.message);
        sendJson(res, 502, { error: `Failed to learn knowledge: ${error.message}` });
      }
    });

    req.on("error", () => {
      sendJson(res, 400, { error: "Request stream error." });
    });
    return;
  }

  // Proxy knowledge list from Python server
  if (url.pathname === "/api/python-knowledge" && req.method === "GET") {
    const limitParam = url.searchParams.get("limit");
    const limit = Math.min(parseInt(limitParam || "50", 10) || 50, 200);
    const files = await pyGetKnowledge(limit);
    if (files === null) {
      sendJson(res, 503, { error: "Python knowledge server unavailable" });
    } else {
      sendJson(res, 200, { files, count: files.length, source: "python-knowledge-server" });
    }
    return;
  }

  // Proxy search against Python knowledge server.
  if (url.pathname === "/api/knowledge/search" && req.method === "GET") {
    const q = (url.searchParams.get("q") || "").trim();
    if (!q) {
      sendJson(res, 400, { error: "Missing query parameter 'q'." });
      return;
    }
    const limit = Math.min(parseInt(url.searchParams.get("limit") || "8", 10) || 8, 20);
    const payload = await pySearchKnowledge(q, limit);
    if (!payload) {
      sendJson(res, 503, { error: "Python knowledge search unavailable" });
    } else {
      sendJson(res, 200, { ...payload, source: "python-knowledge-search" });
    }
    return;
  }

  serveStatic(req, res, url.pathname);
});

// ---------------------------------------------------------------------------
// Python knowledge server subprocess
// ---------------------------------------------------------------------------

let pythonProcess = null;
let isShuttingDown = false;

function startPythonServer() {
  if (!START_PYTHON_SERVER) {
    console.log("[main] Python server disabled (START_PYTHON_SERVER=false)");
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    const pythonScript = path.join(__dirname, "knowledge_server.py");
    
    if (!fs.existsSync(pythonScript)) {
      console.error(`[main] WARNING: knowledge_server.py not found at ${pythonScript}`);
      resolve(); // Continue anyway; Python might not be needed for Render
      return;
    }

    console.log(`[main] Starting Python knowledge server on port ${PYTHON_PORT}...`);
    
    pythonProcess = spawn("python3", [pythonScript], {
      env: {
        ...process.env,
        PYTHON_PORT: String(PYTHON_PORT),
        PYTHON_HOST,
        FTP_HOST: process.env.FTP_HOST || "ftp.geocities.ws",
        FTP_USER: process.env.FTP_USER || "PeakeCoin",
        FTP_PASSWORD: process.env.FTP_PASSWORD || "Peake410",
        FTP_BRAIN_DIR,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });

    let pythonReady = false;

    pythonProcess.stdout.on("data", (data) => {
      const msg = data.toString().trim();
      console.log(`[python] ${msg}`);
      if (msg.includes("running on port")) {
        pythonReady = true;
        if (!pythonReady) resolve();
      }
    });

    pythonProcess.stderr.on("data", (data) => {
      console.error(`[python-err] ${data.toString().trim()}`);
    });

    pythonProcess.on("error", (err) => {
      console.error(`[main] Failed to spawn Python: ${err.message}`);
      reject(err);
    });

    pythonProcess.on("exit", (code) => {
      console.log(`[main] Python server exited with code ${code}`);
      pythonProcess = null;
    });

    // Give Python a moment to start, then resolve regardless
    setTimeout(() => resolve(), 1500);
  });
}

function stopPythonServer() {
  if (pythonProcess) {
    console.log("[main] Terminating Python server...");
    pythonProcess.kill("SIGTERM");
    setTimeout(() => {
      if (pythonProcess) {
        pythonProcess.kill("SIGKILL");
      }
    }, 3000);
  }
}

function shutdown(signalName) {
  if (isShuttingDown) {
    console.log(`[main] ${signalName} received again; shutdown already in progress.`);
    return;
  }

  isShuttingDown = true;
  console.log(`[main] ${signalName} received, shutting down...`);
  stopPythonServer();

  server.close(() => {
    console.log("[main] Server closed.");
    process.exit(0);
  });
}

// Start Node.js server after Python is ready
(async () => {
  try {
    await startPythonServer();
  } catch (err) {
    console.error(`[main] Could not start Python: ${err.message}`);
  }

  server.listen(PORT, () => {
    ensureDirectories();
    console.log(`[main] AI Memory server running at http://localhost:${PORT}`);
    console.log("[main] Mode: Python memory engine only");
    console.log(`[main] Python knowledge server: ${PYTHON_KNOWLEDGE_SERVER}`);
    console.log(`[main] Knowledge store (primary): FTP ${FTP_BRAIN_DIR}`);
    console.log(`[main] Local knowledge dir (legacy/cache only): ${KNOWLEDGE_DIR}`);
  });
})();

// Graceful shutdown
process.on("SIGTERM", () => shutdown("SIGTERM"));

process.on("SIGINT", () => shutdown("SIGINT"));

