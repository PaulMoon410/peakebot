const IPFS_GATEWAY = "https://ipfs.io/ipfs/";
const LLAMA_SERVER = "http://74.208.146.37:8080";
const RENDER_SERVER = "https://peakebot.onrender.com";
const CONNECTION_CHECK_INTERVAL_MS = 15000;
const DIRECT_LLAMA_ALLOWED = window.location.protocol === "http:" || LLAMA_SERVER.startsWith("https://");

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
  llamaConnected: false,
  nodeConnected: false,
  lastFtpStatus: "",
  connectionCheckTimer: null,
};

function hasAiConnection() {
  return state.llamaConnected || state.nodeConnected;
}

function updateChatAvailability() {
  const online = hasAiConnection();

  if (elements.chatInput) {
    elements.chatInput.disabled = !online;
    if (!online) {
      elements.chatInput.placeholder = "Waiting for AI connection (Llama or Render Node)...";
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
  
  if (state.llamaConnected && state.nodeConnected) {
    icon = "🟢";
    text = "Connected (Llama + Node Server)";
    bg = "#2a3a2a";
  } else if (state.llamaConnected) {
    icon = "🟡";
    text = "Connected (Llama Direct)";
    bg = "#3a3a2a";
  } else if (state.nodeConnected) {
    icon = "🟡";
    text = "Connected (Node Server)";
    bg = "#3a3a2a";
  }
  
  if (elements.connectionStatus) {
    elements.connectionStatus.textContent = `${icon} ${text}`;
    elements.connectionStatus.style.backgroundColor = bg;
  }

  if (elements.llamaStatus) {
    if (!DIRECT_LLAMA_ALLOWED) {
      elements.llamaStatus.textContent = "Llama: Blocked on HTTPS (mixed content)";
      elements.llamaStatus.className = "service-indicator offline";
    } else {
      elements.llamaStatus.textContent = state.llamaConnected ? "Llama: Connected" : "Llama: Offline";
      elements.llamaStatus.className = `service-indicator ${state.llamaConnected ? "online" : "offline"}`;
    }
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
  elements.chatLog.prepend(wrapper);
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
    `AI Server: ${LLAMA_SERVER}`,
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
    const conversationHistory = state.memory.conversations.slice(-10).map((conv) => ([
      { role: "user", content: conv.user },
      { role: "assistant", content: conv.ai },
    ])).flat();

    let response;
    
    // Try direct Llama only when browser security allows it.
    if (DIRECT_LLAMA_ALLOWED && state.llamaConnected) {
      try {
        response = await fetch(`${LLAMA_SERVER}/v1/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: [
              ...conversationHistory,
              { role: "user", content: text },
            ],
            model: "qwen2.5",
            stream: false,
          }),
        });

        if (response.ok) {
          const data = await response.json();
          if (data.choices && data.choices[0] && data.choices[0].message) {
            return data.choices[0].message.content;
          }
        } else {
          state.llamaConnected = false;
          setConnectionStatus();
        }
      } catch (error) {
        state.llamaConnected = false;
        setConnectionStatus();
      }
    }
    
    // Fall back to Render Node server
    if (state.nodeConnected) {
      try {
        const nodeResponse = await fetch(`${RENDER_SERVER}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            prompt: text,
            memory: state.memory,
          }),
        });

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
            }
            console.log(`[chat] ${state.lastFtpStatus}`);
          }
          return data.response;
        }

        if (!nodeResponse.ok) {
          const message = data?.error || `Node proxy error (${nodeResponse.status}).`;
          return message;
        }

        return "Node server returned an unexpected response.";
      } catch (error) {
        state.nodeConnected = false;
        setConnectionStatus();
      }
    }
    
    return "AI service unavailable. Please check connection status.";
  } catch (error) {
    return `Error: ${error.message}`;
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
    setStatus("AI is offline. Waiting for Llama or Render Node connection.", true);
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
  setStatus("AI is thinking...");
  
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
  addMessage("ai", "Hello! I'm powered by Qwen AI and store memories in your browser. Ask me anything, remember facts, save notes, or load JSON memory.");
}

async function checkConnections() {
  // Direct Llama checks are blocked by browser mixed-content policy on HTTPS pages.
  if (!DIRECT_LLAMA_ALLOWED) {
    state.llamaConnected = false;
  } else {
    try {
      const response = await fetch(`${LLAMA_SERVER}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: "user", content: "test" }],
          model: "qwen2.5",
          stream: false,
        }),
      });
      state.llamaConnected = response.ok;
    } catch {
      state.llamaConnected = false;
    }
  }
  
  // Check Render Node server health independent of model status.
  try {
    const response = await fetch(`${RENDER_SERVER}/api/health`, {
      method: "GET",
      cache: "no-store",
    });
    state.nodeConnected = response.ok;
  } catch {
    state.nodeConnected = false;
  }

  setConnectionStatus();
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
  elements.loadUrlButton.addEventListener("click", handleRemoteLoad);
  elements.importFile.addEventListener("change", handleFileImport);
}

registerEvents();
initializeMemory();
seedWelcomeMessage();
