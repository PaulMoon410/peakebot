const IPFS_GATEWAY = "https://ipfs.io/ipfs/";
const RENDER_SERVER = "https://peakebot.onrender.com";
const CONNECTION_CHECK_INTERVAL_MS = 15000;

const elements = {
  chatLog: document.querySelector("#chat-log"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  chatSubmit: document.querySelector("#chat-form button[type='submit']"),
  memoryPreview: document.querySelector("#memory-preview"),
  status: document.querySelector("#status"),
  connectionStatus: document.querySelector("#connection-status"),
  llamaStatus: document.querySelector("#llama-status"),
  nodeStatus: document.querySelector("#node-status"),
  memoryStats: document.querySelector("#memory-stats"),
  exportMemory: document.querySelector("#export-memory"),
  copyMemory: document.querySelector("#copy-memory"),
  clearMemory: document.querySelector("#clear-memory"),
  loadUrl: document.querySelector("#load-url"),
  loadUrlButton: document.querySelector("#load-url-button"),
  importFile: document.querySelector("#import-file"),
};

const defaultMemory = () => ({
  profile: {
    siteOrigin: window.location.origin,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  },
  facts: [],
  notes: [],
  conversations: [],
  remoteSources: [],
});

const state = {
  memory: defaultMemory(),
  serverAvailable: true,
  pythonConnected: false,
  nodeConnected: false,
  lastFtpStatus: "",
  startupKnowledgeCount: 0,
  connectionCheckTimer: null,
};

function hasAiConnection() {
  return state.nodeConnected;
}

function updateChatAvailability() {
  const online = hasAiConnection();

  if (elements.chatInput) {
    elements.chatInput.disabled = !online;
    if (!online) {
      elements.chatInput.placeholder = "Waiting for Python memory engine connection...";
    }
  }

  if (elements.chatSubmit) {
    elements.chatSubmit.disabled = !online;
  }
}

function persistMemory() {
  state.memory.profile.updatedAt = new Date().toISOString();
  renderMemory();
  // Sync to Render server (fire-and-forget)
  if (state.nodeConnected) {
    fetch(`${RENDER_SERVER}/api/memory`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.memory),
    }).catch(() => { /* non-critical */ });
  }
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.style.color = isError ? "#ff9aa9" : "#aab2d5";
}

function setConnectionStatus() {
  let icon = "🔴";
  let text = "Offline";
  let bg = "#3a2a2a";
  
  if (state.pythonConnected && state.nodeConnected) {
    icon = "🟢";
    text = "Connected (Python Memory + Node Server)";
    bg = "#2a3a2a";
  } else if (state.nodeConnected) {
    icon = "🟡";
    text = "Connected (Node Proxy Only)";
    bg = "#3a3a2a";
  }
  
  if (elements.connectionStatus) {
    elements.connectionStatus.textContent = `${icon} ${text}`;
    elements.connectionStatus.style.backgroundColor = bg;
  }

  if (elements.llamaStatus) {
    elements.llamaStatus.textContent = state.pythonConnected ? "Python Engine: Connected" : "Python Engine: Offline";
    elements.llamaStatus.className = `service-indicator ${state.pythonConnected ? "online" : "offline"}`;
  }

  if (elements.nodeStatus) {
    elements.nodeStatus.textContent = state.nodeConnected ? "Node Server: Connected" : "Node Server: Offline";
    elements.nodeStatus.className = `service-indicator ${state.nodeConnected ? "online" : "offline"}`;
  }

  updateChatAvailability();
}

function addMessage(role, text) {
  const wrapper = document.createElement("article");
  wrapper.className = `message ${role}`;
  wrapper.innerHTML = `<strong>${role === "user" ? "You" : "AI"}</strong><p>${escapeHtml(text)}</p>`;
  elements.chatLog.appendChild(wrapper);
  // Auto-scroll to bottom
  setTimeout(() => {
    elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
  }, 0);
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMemory() {
  elements.memoryPreview.textContent = JSON.stringify(state.memory, null, 2);
  const stats = [
    `Storage: ${state.nodeConnected ? "Synced to Render server" : "Runtime only (server offline)"}`,
    `Engine: Python memory retrieval`,
    `Facts: ${state.memory.facts.length}`,
    `Notes: ${state.memory.notes.length}`,
    `Conversations: ${state.memory.conversations.length}`,
    `Remote sources: ${state.memory.remoteSources.length}`,
    `Updated: ${state.memory.profile.updatedAt}`,
  ];
  elements.memoryStats.innerHTML = stats.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function rememberFact(subject, value, source = "user") {
  const existing = state.memory.facts.find((item) => item.subject.toLowerCase() === subject.toLowerCase());
  if (existing) {
    existing.value = value;
    existing.updatedAt = new Date().toISOString();
    existing.source = source;
  } else {
    state.memory.facts.push({
      subject,
      value,
      source,
      updatedAt: new Date().toISOString(),
    });
  }
  persistMemory();
}

function addNote(text, source = "user") {
  state.memory.notes.push({
    text,
    source,
    timestamp: new Date().toISOString(),
  });
  persistMemory();
}

function searchFacts(query) {
  const q = query.toLowerCase();
  return state.memory.facts.filter((item) => {
    return item.subject.toLowerCase().includes(q) || item.value.toLowerCase().includes(q);
  });
}

function searchNotes(query) {
  const q = query.toLowerCase();
  return state.memory.notes.filter((item) => item.text.toLowerCase().includes(q));
}

function normalizeIpfsUrl(input) {
  const trimmed = input.trim();
  if (trimmed.startsWith("ipfs://")) {
    return `${IPFS_GATEWAY}${trimmed.replace("ipfs://", "")}`;
  }
  if (/^[a-z0-9]{46,}$/i.test(trimmed) && !trimmed.startsWith("http")) {
    return `${IPFS_GATEWAY}${trimmed}`;
  }
  return trimmed;
}

async function loadRemoteJson(input) {
  const url = normalizeIpfsUrl(input);
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load JSON: ${response.status}`);
  }
  return response.json();
}

function mergeRemoteMemory(remoteMemory, source) {
  if (!remoteMemory || typeof remoteMemory !== "object") {
    throw new Error("Remote JSON was not an object.");
  }

  if (Array.isArray(remoteMemory.facts)) {
    for (const fact of remoteMemory.facts) {
      if (fact.subject && fact.value) {
        rememberFact(fact.subject, fact.value, source);
      }
    }
  }

  if (Array.isArray(remoteMemory.notes)) {
    for (const note of remoteMemory.notes) {
      if (note.text) {
        addNote(note.text, source);
      }
    }
  }

  if (Array.isArray(remoteMemory.conversations)) {
    const incoming = remoteMemory.conversations.slice(-20);
    state.memory.conversations.push(...incoming);
  }

  state.memory.remoteSources.push({
    source,
    loadedAt: new Date().toISOString(),
  });

  persistMemory();
}

function exportMemory() {
  const blob = new Blob([JSON.stringify(state.memory, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "frontend-ai-memory.json";
  anchor.click();
  URL.revokeObjectURL(url);
}

async function copyMemory() {
  await navigator.clipboard.writeText(JSON.stringify(state.memory, null, 2));
}

function clearMemory() {
  state.memory = defaultMemory();
  persistMemory();
  elements.chatLog.innerHTML = "";
}

async function generateReply(prompt) {
  const text = prompt.trim();
  const lower = text.toLowerCase();
  state.lastFtpStatus = "";

  const rememberMatch = lower.match(/^remember that (.+?) is (.+)$/i) || lower.match(/^remember (.+?) is (.+)$/i);
  if (rememberMatch) {
    const [, subject, value] = rememberMatch;
    rememberFact(subject.trim(), value.trim());
    return `Stored in memory: ${subject.trim()} = ${value.trim()}.`;
  }

  const noteMatch = lower.match(/^note[:\s]+(.+)$/i);
  if (noteMatch) {
    addNote(text.slice(text.toLowerCase().indexOf(noteMatch[1])));
    return "Saved that note to website-side memory.";
  }

  const whatDoYouRemember = lower.match(/what do you remember about (.+)/i);
  if (whatDoYouRemember) {
    const query = whatDoYouRemember[1].trim();
    const facts = searchFacts(query);
    const notes = searchNotes(query);
    if (!facts.length && !notes.length) {
      return `I do not have anything stored yet about ${query}.`;
    }

    const factText = facts.map((item) => `${item.subject} = ${item.value}`).join("; ");
    const noteText = notes.map((item) => item.text).join("; ");
    return [factText, noteText].filter(Boolean).join(" | ");
  }

  if (lower.includes("show memory") || lower.includes("export memory")) {
    return "Use the buttons on the left to export or copy the JSON memory.";
  }

  try {
    // Chat runs through the Render Node proxy to the Python memory engine.
    if (state.nodeConnected) {
      try {
        const sendChatRequest = async () => {
          return fetch(`${RENDER_SERVER}/api/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              prompt: text,
              memory: state.memory,
            }),
          });
        };

        let nodeResponse = await sendChatRequest();

        // One retry for transient deploy/restart windows.
        if (nodeResponse.status === 502 || nodeResponse.status === 503 || nodeResponse.status === 504) {
          await new Promise((resolve) => setTimeout(resolve, 2500));
          nodeResponse = await sendChatRequest();
        }

        let data = null;
        try {
          data = await nodeResponse.json();
        } catch {
          // Non-JSON response; handled below with generic message.
        }

        if (nodeResponse.ok && data?.response) {
          if (data.ftp) {
            if (data.ftp.skipped) {
              state.lastFtpStatus = "FTP: skipped (duplicate response).";
            } else if (data.ftp.ok) {
              const target = data.ftp.dailyFile || "daily file";
              state.lastFtpStatus = `FTP: save success (${target}).`;
            } else {
              const reason = data.ftp.error || "unknown FTP error";
              state.lastFtpStatus = `FTP: save failed (${reason}).`;
              // Verify FTP issue is real - retry verification
              try {
                const verifyCheck = await fetch(`${RENDER_SERVER}/api/health`, {
                  method: "GET",
                  headers: { "Content-Type": "application/json" },
                });
                const verifyData = await verifyCheck.json();
                if (!verifyData.pythonKnowledgeServerOnline) {
                  state.lastFtpStatus = `FTP: verified offline (${reason}).`;
                } else {
                  state.lastFtpStatus = `FTP: temporary issue (${reason}), retrying...`;
                }
              } catch {
                state.lastFtpStatus = `FTP: save failed, network error (${reason}).`;
              }
            }
            console.log(`[chat] ${state.lastFtpStatus}`);
          }
          return data.response;
        }

        if (!nodeResponse.ok) {
          const code = nodeResponse.status;
          const reason = data?.error || `Node proxy error (${code}).`;
          state.lastFtpStatus = `Backend temporary issue (${code}).`;
          if (code === 503) {
            return "Service is restarting. Please retry in a few seconds and I will answer.";
          }
          if (code === 502 || code === 504) {
            return "I hit a temporary backend timeout. Please send your message again and I will respond.";
          }
          return `I could not reach the AI backend right now: ${reason}`;
        }

        return "Node server returned an unexpected response.";
      } catch (error) {
        state.nodeConnected = false;
        setConnectionStatus();
        return "Connection to the AI backend dropped temporarily. Please retry in a few seconds.";
      }
    }

    return "AI backend is currently offline. I can still store notes/facts, and you can retry chat shortly.";
  } catch (error) {
    return "Unexpected chat error occurred. Please retry your message.";
  }
}

function logConversation(prompt, response) {
  state.memory.conversations.push({
    timestamp: new Date().toISOString(),
    user: prompt,
    ai: response,
  });
  state.memory.conversations = state.memory.conversations.slice(-100);
  persistMemory();
}

async function handleChatSubmit(event) {
  event.preventDefault();

  if (!hasAiConnection()) {
    setStatus("AI is offline. Waiting for Python memory engine connection.", true);
    await checkConnections();
    if (!hasAiConnection()) {
      return;
    }
  }

  const prompt = elements.chatInput.value.trim();
  if (!prompt) {
    return;
  }

  addMessage("user", prompt);
  elements.chatInput.value = "";
  setStatus("Searching knowledge for relevant information... Please be patient.");
  
  try {
    const response = await generateReply(prompt);
    addMessage("ai", response);
    logConversation(prompt, response);
    const ftpNote = state.lastFtpStatus ? ` ${state.lastFtpStatus}` : "";
    setStatus(`Response received and saved to memory.${ftpNote}`);
  } catch (error) {
    addMessage("ai", `Error: ${error.message}`);
    setStatus(`Chat error: ${error.message}`, true);
  }
}

async function handleRemoteLoad() {
  const input = elements.loadUrl.value.trim();
  if (!input) {
    setStatus("Enter a URL or IPFS CID first.", true);
    return;
  }

  try {
    setStatus("Loading remote JSON...");
    const remoteMemory = await loadRemoteJson(input);
    mergeRemoteMemory(remoteMemory, normalizeIpfsUrl(input));
    setStatus("Remote JSON merged into website-side memory.");
  } catch (error) {
    setStatus(error.message, true);
  }
}

function handleFileImport(event) {
  const file = event.target.files?.[0];
  if (!file) {
    return;
  }

  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(String(reader.result));
      mergeRemoteMemory(parsed, `file:${file.name}`);
      setStatus(`Imported ${file.name}.`);
    } catch (error) {
      setStatus(`Invalid JSON file: ${error.message}`, true);
    }
  };
  reader.readAsText(file);
}

function seedWelcomeMessage() {
  addMessage("ai", "Hello! I use a Python memory engine with FTP-backed recall. Ask me anything, remember facts, or save notes.");
}

async function checkConnections() {
  // Check Render Node server and Python engine status.
  try {
    const response = await fetch(`${RENDER_SERVER}/api/health`, {
      method: "GET",
      cache: "no-store",
    });
    state.nodeConnected = response.ok;
    if (response.ok) {
      const health = await response.json();
      state.pythonConnected = Boolean(health.pythonKnowledgeServerOnline);
    } else {
      state.pythonConnected = false;
    }
  } catch {
    state.nodeConnected = false;
    state.pythonConnected = false;
  }

  setConnectionStatus();
}

async function warmupKnowledgeFacts() {
  if (!state.nodeConnected) {
    return;
  }

  try {
    setStatus("Searching knowledge base facts on startup... Please be patient.");
    const query = encodeURIComponent("bitcoin hive constitution america maryland chessie");
    const response = await fetch(`${RENDER_SERVER}/api/knowledge/search?q=${query}&limit=10`, {
      method: "GET",
      cache: "no-store",
    });

    if (!response.ok) {
      return;
    }

    const data = await response.json();
    const count = Number(data?.count || 0);
    state.startupKnowledgeCount = count;
    setStatus(`Startup knowledge scan complete. ${count} fact matches ready.`);
  } catch {
    // Non-fatal: chat should still function without startup warm-up.
  }
}

function startConnectionMonitor() {
  if (state.connectionCheckTimer) {
    clearInterval(state.connectionCheckTimer);
  }

  state.connectionCheckTimer = setInterval(() => {
    checkConnections();
  }, CONNECTION_CHECK_INTERVAL_MS);

  window.addEventListener("online", () => {
    checkConnections();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      checkConnections();
    }
  });
}

async function initializeMemory() {
  state.memory = defaultMemory();
  updateChatAvailability();
  await checkConnections();
  await warmupKnowledgeFacts();
  startConnectionMonitor();

  // Load persisted memory from Render server
  if (state.nodeConnected) {
    try {
      const res = await fetch(`${RENDER_SERVER}/api/memory`, { cache: "no-store" });
      if (res.ok) {
        const remote = await res.json();
        if (remote && typeof remote === "object") {
          state.memory = { ...defaultMemory(), ...remote };
          setStatus("Memory loaded from server.");
        }
      }
    } catch {
      setStatus("Could not load remote memory — using runtime defaults.", true);
    }
  } else {
    setStatus("Using runtime memory only (server offline).");
  }

  renderMemory();
}

function registerEvents() {
  elements.chatForm.addEventListener("submit", handleChatSubmit);
  elements.chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      elements.chatForm.requestSubmit();
    }
  });
  elements.exportMemory.addEventListener("click", exportMemory);
  elements.copyMemory.addEventListener("click", async () => {
    try {
      await copyMemory();
      setStatus("Memory JSON copied to clipboard.");
    } catch (error) {
      setStatus(`Clipboard failed: ${error.message}`, true);
    }
  });
  elements.clearMemory.addEventListener("click", () => {
    clearMemory();
    setStatus("Website-side memory cleared.");
  });
  if (elements.loadUrlButton) {
    elements.loadUrlButton.addEventListener("click", handleRemoteLoad);
  }
  if (elements.importFile) {
    elements.importFile.addEventListener("change", handleFileImport);
  }
}

registerEvents();
initializeMemory();
seedWelcomeMessage();
