# Peakecoin AI - Memory Chat Interface

An AI chat application with persistent memory and automatic knowledge saving to FTP. Features real-time connection status, duplicate response detection, and a split deployment: static frontend on Geocities, Node.js backend on Render.

## Architecture

- **Frontend**: Static files hosted on geocities.ws/peakecoin/ai
- **Backend**: Node.js server on Render (handles chat, saves conversations to FTP)
- **Knowledge Base**: FTP storage at ftp.geocities.ws/ai/brain/
- **AI Engine**: Llama server integration (74.208.146.37:8080)

## Features

- **AI Chat Interface** - Real-time conversation with Qwen 2.5 LLM
- **Memory Persistence** - Save facts, notes, and conversation history
- **Duplicate Detection** - Checks FTP brain before saving responses
- **FTP Knowledge Base** - Automatic JSON saving to Geocities/ai/brain/
- **Connection Status** - Visual indicator for Llama and Node backend
- **Browser Storage** - LocalStorage for client-side memory backup
- **IPFS Support** - Load memory from URLs or IPFS CIDs

## Quick Start

### Local Development

```bash
# Install dependencies
npm install

# Start server
npm start

# Open browser
http://localhost:3000
```

### Environment Variables

```bash
LLAMA_SERVER=http://74.208.146.37:8080  # Llama API endpoint
PORT=3000                                # Server port
NODE_ENV=development                     # Environment
```

## Deployment

### 1. Deploy Backend to Render

1. **Push to GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/peakecoin-ai.git
   git push -u origin main
   ```

2. **Create Render Web Service**:
   - Go to render.com
   - New → Web Service
   - Connect your GitHub repository
   - Build command: `npm install`
   - Start command: `npm start`
   - Environment variables:
     ```
     LLAMA_SERVER=http://74.208.146.37:8080
     ```

3. **Get Your Render URL**:
   - After deployment, note your URL (e.g., `https://peakecoin-ai.onrender.com`)
   - Update `app.js` line 3: `const RENDER_SERVER = "YOUR_RENDER_URL"`

### 2. Deploy Frontend to Geocities

1. **Prepare files** (3 files only):
   - `index.html`
   - `app.js` (with updated RENDER_SERVER URL)
   - `styles.css`

2. **Upload via FTP**:
   - FTP client or FileZilla
   - Host: ftp.geocities.ws
   - Username: PeakeCoin
   - Password: Peake410
   - Upload to: `/peakecoin/ai/`

3. **Access frontend**:
   - `https://geocities.ws/peakecoin/ai/index.html`

## API Endpoints

### Chat
**POST `/api/chat`**
```json
{
  "prompt": "What is the capital of France?",
  "memory": { ... }
}
```
Returns:
```json
{
  "response": "The capital of France is Paris.",
  "source": "llama",
  "duplicate": false
}
```

### Knowledge
**GET `/api/knowledge`**
Returns last 50 saved conversations

### Memory
**GET `/api/memory`** - Read persisted memory
**PUT `/api/memory`** - Write persisted memory

## Directory Structure

```
├── server.js              # Node.js backend + FTP integration
├── index.html             # Frontend UI
├── app.js                 # Frontend JavaScript (ES modules)
├── styles.css             # Styling
├── package.json           # Node config with basic-ftp dependency
├── .gitignore             # Git exclusions
├── .env.example           # Environment variables
├── README.md              # This file
└── data/
    ├── memory.json        # Persistent memory (local backup)
    └── knowledge/         # Conversation history JSON (local backup)
```

## How It Works

1. **User submits message** at geocities.ws/peakecoin/ai
2. **Frontend** calls Render backend at `/api/chat`
3. **Render server**:
   - Calls Llama server for response
   - Checks FTP/ai/brain/ for duplicate responses
   - If new response, saves to FTP + local storage
   - Returns response to frontend
4. **Frontend** displays response and updates local memory
5. **All conversations** persist in FTP and local backups

## FTP Structure

```
ftp.geocities.ws/
├── peakecoin/ai/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── ai/brain/
│   ├── conversation-2026-05-10T120000-abc123.json
│   ├── conversation-2026-05-10T120500-def456.json
│   └── ... (more conversations)
```

## Chat Commands

- `remember that [subject] is [value]` - Save a fact
- `note: [text]` - Save a note
- `what do you remember about [topic]` - Recall information
- `show memory` - Display current memory as JSON

## JSON Memory Format

```json
{
  "profile": {
    "siteOrigin": "https://geocities.ws/peakecoin/ai",
    "createdAt": "2026-05-10T12:00:00.000Z",
    "updatedAt": "2026-05-10T12:00:00.000Z"
  },
  "facts": [...],
  "notes": [...],
  "conversations": [...],
  "remoteSources": []
}
```

## Connection Status Indicators

- 🔴 **Offline** - No services available
- 🟡 **Connected (Llama Direct)** - Direct CORS connection works
- 🟡 **Connected (Node Server)** - Backend API works
- 🟢 **Connected (Llama + Node Server)** - Both available

## Duplicate Response Detection

- Each conversation saved to FTP includes full response text
- New responses checked against all previous responses
- Prevents redundant saves and improves knowledge base efficiency

## License

MIT
  },
  "facts": [
    {
      "subject": "favorite color",
      "value": "blue",
      "source": "user",
      "updatedAt": "2026-03-14T00:00:00.000Z"
    }
  ],
  "notes": [],
  "conversations": [],
  "remoteSources": []
}
```

## IPFS recall

The loader accepts:
- `ipfs://<CID>`
- `<CID>`
- `https://gateway/path.json`

It resolves IPFS items through the public gateway at `https://ipfs.io/ipfs/`.

## If you want true hosted-file saving

This project now includes that writable layer using a Node.js API and a local JSON file.

For production hosting, replace file storage with durable storage (database, blob storage, or managed key-value store).

