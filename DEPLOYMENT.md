# Deployment Guide

## How Environment Variables Work

**Why aren't passwords in GitHub?**
- GitHub is public (or could be)
- We use **Environment Variables** instead
- Set them in Render dashboard, NOT in code
- Render keeps them private and passes them to the running app
- The app reads them from `process.env`

**Example:**
- Code: `const password = process.env.FTP_PASSWORD`
- GitHub: empty (not set)
- Render Dashboard: You set `FTP_PASSWORD = Peake410`
- Running App: Reads the value from Render

## Step 1: Deploy Backend to Render

### 1.1 Push to GitHub
```bash
cd /home/moon/Desktop/webpage
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/peakecoin-ai.git
git push -u origin main
```

### 1.2 Create Render Service
1. Go to https://render.com
2. Click "New +"
3. Select "Web Service"
4. Connect your GitHub account and select the repository
5. Configure:
   - **Name**: peakebot
   - **Environment**: Node
   - **Build Command**: `npm install`
   - **Start Command**: `npm start`
   - **Plan**: Free tier is fine
6. Add Environment Variables (click "Advanced" → "Add Environment Variable"):
   - `LLAMA_SERVER` = `http://74.208.146.37:8080`
   - `FTP_HOST` = `ftp.geocities.ws`
   - `FTP_USER` = `PeakeCoin`
   - `FTP_PASSWORD` = `Peake410`
7. Click "Create Web Service"
8. Wait for deployment (5-10 minutes)
9. **Copy your Render URL** (should be `https://peakebot.onrender.com`)

## Step 2: Update Frontend with Render URL

(Already done! `app.js` is already set to `https://peakebot.onrender.com`)

Commit and push:
```bash
git add .
git commit -m "Ready for Render deployment"
git push origin main
```

## Step 3: Deploy Frontend to Geocities

### 3.1 Prepare Files
You need only 3 files:
- `index.html`
- `app.js` (with updated RENDER_SERVER)
- `styles.css`

### 3.2 Upload via FTP

**Using FileZilla (recommended):**
1. Download FileZilla from https://filezilla-project.org/
2. File → Site Manager → New Site
3. Configure:
   - **Host**: ftp.geocities.ws
   - **Protocol**: FTP
   - **User**: PeakeCoin
   - **Password**: Peake410
4. Connect
5. Navigate to `/peakecoin/ai/` (create `/ai/` folder if needed)
6. Drag and drop the 3 files

**Or using command line:**
```bash
ftp ftp.geocities.ws
# Login with: PeakeCoin / Peake410
cd peakecoin/ai
put index.html
put app.js
put styles.css
quit
```

### 3.3 Access Your App
- Visit: https://geocities.ws/peakecoin/ai/index.html

## Step 4: Test Everything

1. Open https://geocities.ws/peakecoin/ai/index.html
2. Check connection status (should show 🟢 Connected)
3. Send a message
4. Watch the AI respond
5. Check FTP for saved conversations at `ai/brain/`

## Troubleshooting

**"Connection status is 🔴 Offline"**
- Render backend not running: Check render.com dashboard
- Llama server not responding: Check 74.208.146.37:8080

**"Error: Payload too large"**
- Message is too long
- Shorten your input

**"Response not saving to FTP"**
- FTP credentials incorrect
- Check Geocities FTP settings
- ai/brain/ directory may not exist (server creates it)

**CORS errors in browser console**
- This is expected if Llama CORS isn't enabled
- The Render fallback will handle it

## Monitoring

### Check Render Logs
1. Go to https://render.com/dashboard
2. Click on your service
3. View "Logs" tab for errors

### Check FTP Brain Directory
1. Connect to FTP (as above)
2. Navigate to `ai/brain/`
3. Files are named: `conversation-TIMESTAMP-RANDOM.json`

## File Structure on Geocities

```
ftp.geocities.ws/
├── peakecoin/
│   ├── ai/
│   │   ├── index.html
│   │   ├── app.js
│   │   └── styles.css
├── ai/
│   └── brain/
│       ├── conversation-2026-05-10T120000-abc123.json
│       └── ... (more conversations)
```

## Next Steps

- Monitor knowledge base growth in `ai/brain/`
- Update app.js prompt messages as needed
- Scale Render plan if needed
- Consider backup strategy for FTP files
