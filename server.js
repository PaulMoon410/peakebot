const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const { spawn } = require("child_process");
const Client = require("basic-ftp").Client;

const PORT = process.env.PORT || 3000;
const LLAMA_SERVER = process.env.LLAMA_SERVER || "http://74.208.146.37:8080";
const PYTHON_KNOWLEDGE_SERVER = process.env.PYTHON_KNOWLEDGE_SERVER || "http://localhost:5001";
const PYTHON_PORT = parseInt(process.env.PYTHON_PORT || "5001", 10);
const START_PYTHON_SERVER = process.env.START_PYTHON_SERVER !== "false";

// FTP Configuration from environment variables
const FTP_CONFIG = {
  host: process.env.FTP_HOST || "ftp.geocities.ws",
  user: process.env.FTP_USER || "PeakeCoin",
  password: process.env.FTP_PASSWORD || "Peake410",
};

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
 * POST a knowledge entry to the Python server.
 * Returns true on success, false if the Python server is unavailable.
 */
async function pyStoreKnowledge(userMessage, aiResponse, memoryState) {
  return new Promise((resolve) => {
    const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/knowledge`);
    const transport = pyUrl.protocol === "https:" ? https : http;
    const payload = JSON.stringify({
      user_message: userMessage,
      ai_response: aiResponse,
      memory_state: memoryState,
      check_duplicate: true,
    });

    const options = {
      hostname: pyUrl.hostname,
      port: pyUrl.port || 5001,
      path: pyUrl.pathname,
      method: "POST",
      timeout: 10000,
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(payload),
      },
    };

    const req = transport.request(options, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          console.log(`[py-bridge] Knowledge stored: ${body.filename || "(duplicate)"}`);
        } catch (_) { /* ignore parse error */ }
        resolve(true);
      });
    });

    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
    req.write(payload);
    req.end();
  });
}

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
 * Search past knowledge via Python server.
 * Returns matching entries array (may be empty).
 */
async function pySearchKnowledge(query, limit = 5) {
  return new Promise((resolve) => {
    const encoded = encodeURIComponent(query);
    const pyUrl = new URL(`${PYTHON_KNOWLEDGE_SERVER}/knowledge/search?q=${encoded}&limit=${limit}`);
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
          resolve(body.results || []);
        } catch (_) {
          resolve([]);
        }
      });
    });

    req.on("timeout", () => { req.destroy(); resolve([]); });
    req.on("error", () => resolve([]));
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

async function connectFTP() {
  const client = new Client();
  await client.access(FTP_CONFIG);
  return client;
}

async function isDuplicateResponse(response) {
  try {
    const client = await connectFTP();
    const files = await client.list("/ai/brain");
    
    for (const file of files) {
      if (file.isFile && file.name.endsWith(".json")) {
        const data = await client.downloadToString(`/ai/brain/${file.name}`);
        const json = JSON.parse(data);
        if (json.ai_response === response) {
          client.close();
          return true;
        }
      }
    }
    client.close();
    return false;
  } catch (error) {
    console.error("Error checking for duplicates:", error.message);
    return false;
  }
}

async function saveToBrain(userMessage, aiResponse, memory) {
  try {
    const client = await connectFTP();
    
    // Ensure ai/brain directory exists
    try {
      await client.cd("/ai/brain");
    } catch {
      await client.ensureDir("/ai/brain");
    }

    const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, -5);
    const filename = `conversation-${timestamp}-${Math.random().toString(36).substring(7)}.json`;
    
    const conversation = {
      timestamp: new Date().toISOString(),
      user_message: userMessage,
      ai_response: aiResponse,
      memory_state: memory,
    };

    const jsonData = JSON.stringify(conversation, null, 2);
    await client.uploadFrom(Buffer.from(jsonData), `/ai/brain/${filename}`);
    
    client.close();
    console.log(`Saved to FTP: ${filename}`);
    return filename;
  } catch (error) {
    console.error("Error saving to FTP:", error.message);
    // Fall back to local save
    return null;
  }
}

async function callLlamaServer(messages) {
  return new Promise((resolve, reject) => {
    const llamaUrl = new URL(`${LLAMA_SERVER}/v1/chat/completions`);
    const transport = llamaUrl.protocol === "https:" ? https : http;
    const payload = JSON.stringify({
      messages,
      model: "qwen2.5",
      stream: false,
    });

    const options = {
      hostname: llamaUrl.hostname,
      port: llamaUrl.port || 8080,
      path: llamaUrl.pathname + llamaUrl.search,
      method: "POST",
      timeout: 20000,
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(payload),
      },
    };

    const proxyReq = transport.request(options, (proxyRes) => {
      const chunks = [];
      proxyRes.on("data", (chunk) => chunks.push(chunk));
      proxyRes.on("end", () => {
        try {
          const body = Buffer.concat(chunks).toString("utf8");
          if (proxyRes.statusCode < 200 || proxyRes.statusCode >= 300) {
            reject(new Error(`Upstream Llama returned ${proxyRes.statusCode}: ${body.slice(0, 180)}`));
            return;
          }
          const data = JSON.parse(body);
          if (data.choices && data.choices[0] && data.choices[0].message) {
            resolve(data.choices[0].message.content);
          } else {
            reject(new Error("Invalid response from Llama server"));
          }
        } catch (error) {
          reject(error);
        }
      });
    });

    proxyReq.on("timeout", () => {
      proxyReq.destroy(new Error("Llama upstream timed out"));
    });
    proxyReq.on("error", reject);
    proxyReq.write(payload);
    proxyReq.end();
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
      llamaConfigured: Boolean(LLAMA_SERVER),
      llamaServer: LLAMA_SERVER,
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

        // Try to call Llama server
        try {
          const conversationHistory = (memory.conversations || []).slice(-10).map((conv) => [
            { role: "user", content: conv.user },
            { role: "assistant", content: conv.ai },
          ]).flat();

          // Fetch relevant past knowledge from Python server to augment context
          let knowledgeContext = "";
          try {
            const relevant = await pySearchKnowledge(text, 5);
            if (relevant.length > 0) {
              const snippets = relevant
                .filter((item) => item.user_message && item.ai_response)
                .map((item) => `Q: ${item.user_message.trim()}\nA: ${item.ai_response.trim()}`)
                .join("\n\n");
              if (snippets) {
                knowledgeContext =
                  "The following are relevant past conversations you have had. Use them to inform your answer:\n\n"
                  + snippets + "\n\n";
              }
            }
          } catch (_) { /* non-critical */ }

          const systemMessage = {
            role: "system",
            content: (
              "You are a helpful AI assistant with a persistent memory stored on FTP. "
              + "You learn from every conversation and become wiser over time.\n\n"
              + knowledgeContext
            ).trim(),
          };

          const response = await callLlamaServer([
            systemMessage,
            ...conversationHistory,
            { role: "user", content: text },
          ]);

          // Check for duplicate response
          const isDuplicate = await isDuplicateResponse(response);
          
          if (!isDuplicate) {
            // Save to FTP brain and local knowledge
            await saveToBrain(text, response, memory);
            saveKnowledge({
              user: text,
              ai: response,
              memory,
            });
            // Also forward to Python knowledge server (non-blocking, fire-and-forget)
            pyStoreKnowledge(text, response, memory).then((ok) => {
              if (ok) console.log("[py-bridge] Knowledge forwarded to Python server");
            });
          }

          sendJson(res, 200, { response, source: "llama", duplicate: isDuplicate });
        } catch (llamaError) {
          // If Llama fails, try Python's /chat endpoint as fallback
          console.log(`[main] Llama failed: ${llamaError.message}, trying Python fallback...`);
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
                timeout: 30000,
                headers: {
                  "Content-Type": "application/json",
                  "Content-Length": Buffer.byteLength(pyPayload),
                },
              };

              const pyReq = pyTransport.request(options, (res) => {
                const chunks = [];
                res.on("data", (c) => chunks.push(c));
                res.on("end", () => {
                  const body = Buffer.concat(chunks).toString("utf8");
                  try {
                    resolve({
                      status: res.statusCode,
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

            // Check if Python succeeded
            if (pyResponse.status !== 200) {
              throw new Error(`Python returned ${pyResponse.status}: ${pyResponse.data.error || "unknown error"}`);
            }

            if (!pyResponse.data.response) {
              throw new Error("No response field from Python");
            }

            sendJson(res, 200, {
              response: pyResponse.data.response,
              source: "python-fallback",
              filename: pyResponse.data.filename,
            });
          } catch (pythonError) {
            // Both failed, return error
            console.error(`[main] Both Llama and Python failed:`, llamaError.message, pythonError.message);
            sendJson(res, 502, {
              error: `AI unavailable: Llama (${llamaError.message}) and Python (${pythonError.message}) both failed`,
            });
          }
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

  serveStatic(req, res, url.pathname);
});

// ---------------------------------------------------------------------------
// Python knowledge server subprocess
// ---------------------------------------------------------------------------

let pythonProcess = null;

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
        FTP_HOST: process.env.FTP_HOST || "ftp.geocities.ws",
        FTP_USER: process.env.FTP_USER || "PeakeCoin",
        FTP_PASSWORD: process.env.FTP_PASSWORD || "Peake410",
        LLAMA_SERVER: LLAMA_SERVER,
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
    console.log(`[main] Llama server: ${LLAMA_SERVER}`);
    console.log(`[main] Python knowledge server: ${PYTHON_KNOWLEDGE_SERVER}`);
    console.log(`[main] Knowledge saved to: ${KNOWLEDGE_DIR}`);
  });
})();

// Graceful shutdown
process.on("SIGTERM", () => {
  console.log("[main] SIGTERM received, shutting down...");
  stopPythonServer();
  server.close(() => {
    console.log("[main] Server closed.");
    process.exit(0);
  });
});

process.on("SIGINT", () => {
  console.log("[main] SIGINT received, shutting down...");
  stopPythonServer();
  server.close(() => {
    console.log("[main] Server closed.");
    process.exit(0);
  });
});

