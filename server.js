const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const Client = require("basic-ftp").Client;

const PORT = process.env.PORT || 3000;
const LLAMA_SERVER = process.env.LLAMA_SERVER || "http://74.208.146.37:8080";

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
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(payload),
      },
    };

    const proxyReq = http.request(options, (proxyRes) => {
      const chunks = [];
      proxyRes.on("data", (chunk) => chunks.push(chunk));
      proxyRes.on("end", () => {
        try {
          const body = Buffer.concat(chunks).toString("utf8");
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

    proxyReq.on("error", reject);
    proxyReq.write(payload);
    proxyReq.end();
  });
}

const server = http.createServer((req, res) => {
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

          const response = await callLlamaServer([
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
          }

          sendJson(res, 200, { response, source: "llama", duplicate: isDuplicate });
        } catch (llamaError) {
          // If Llama fails, return error
          sendJson(res, 502, { error: `Llama server error: ${llamaError.message}` });
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

  serveStatic(req, res, url.pathname);
});

server.listen(PORT, () => {
  ensureDirectories();
  console.log(`AI Memory server running at http://localhost:${PORT}`);
  console.log(`Llama server: ${LLAMA_SERVER}`);
  console.log(`Knowledge saved to: ${KNOWLEDGE_DIR}`);
});

