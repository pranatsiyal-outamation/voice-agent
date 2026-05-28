"""
Run locally:  python elevenlabs_dashboard.py
Then open:    http://localhost:8081
Connects to the DB via DATABASE_URL in your .env
"""

import asyncio, asyncpg, json, os
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>ElevenLabs Calls</title>
<style>
  body { font-family: monospace; background: #111; color: #ddd; padding: 20px; }
  button { background: #7c3aed; color: #fff; border: none; padding: 8px 18px; cursor: pointer; border-radius: 4px; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 12px; }
  th { background: #1a1a1a; color: #888; text-align: left; padding: 8px; border-bottom: 1px solid #333; }
  td { padding: 8px; border-bottom: 1px solid #1e1e1e; vertical-align: top; }
  tr:hover td { background: #1a1a1a; cursor: pointer; }
  .transcript { background: #0d0d0d; padding: 10px; font-size: 11px; line-height: 1.8; }
  .agent { color: #a78bfa; } .user { color: #60a5fa; }
  .badge-yes { color: #f87171; font-weight: bold; }
  .badge-no  { color: #555; }
  #info { font-size: 12px; color: #555; margin-left: 10px; }
</style></head>
<body>
<h2 style="margin-bottom:4px">ElevenLabs Call Log</h2>
<p style="color:#555;font-size:11px;margin-bottom:12px">Click a row to expand transcript</p>
<button onclick="load()">Refresh</button><span id="info"></span>
<table>
  <thead><tr>
    <th>#</th>
    <th>Caller</th>
    <th>Contractor</th>
    <th>Shipment ID</th>
    <th>Status</th>
    <th>Follow-up</th>
    <th>Human CB</th>
    <th>Duration</th>
    <th>Ended</th>
  </tr></thead>
  <tbody id="tb"></tbody>
</table>
<script>
async function load() {
  document.getElementById('info').textContent = 'Loading...';
  let rows;
  try {
    const res = await fetch('/api/elevenlabs-calls');
    const data = await res.json();
    if (!res.ok) { document.getElementById('info').textContent = 'Error: ' + (data.error || res.status); return; }
    rows = data;
  } catch(e) {
    document.getElementById('info').textContent = 'Fetch error: ' + e.message;
    return;
  }
  const tb = document.getElementById('tb');
  tb.innerHTML = '';
  rows.forEach((c, i) => {
    const dur = c.duration_secs != null ? c.duration_secs + 's' : '—';
    const hcb = c.human_callback
      ? '<span class="badge-yes">YES</span>'
      : '<span class="badge-no">no</span>';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${c.caller_number||'—'}</td>
      <td>${c.contractor_name||'—'}</td>
      <td>${c.shipment_id||'—'}</td>
      <td style="max-width:120px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${c.shipment_status||'—'}</td>
      <td>${c.follow_up_date||'—'}</td>
      <td>${hcb}</td>
      <td>${dur}</td>
      <td>${c.ended_at||'—'}</td>`;
    tr.onclick = () => toggleTranscript(i, tr, c.transcript);
    tb.appendChild(tr);
  });
  document.getElementById('info').textContent = `${rows.length} calls — ${new Date().toLocaleTimeString()}`;
}

function toggleTranscript(id, tr, transcript) {
  const existing = document.getElementById('t'+id);
  if (existing) { existing.remove(); return; }
  const row = document.createElement('tr'); row.id = 't'+id;
  const td = document.createElement('td'); td.colSpan = 9;
  const box = document.createElement('div'); box.className = 'transcript';
  const items = Array.isArray(transcript) ? transcript : [];
  box.innerHTML = items.length
    ? items.map(m => {
        const role = (m.role || '').toLowerCase();
        const text = m.message || m.text || '<i style="color:#444">audio only</i>';
        const label = role === 'agent' ? 'ARIA' : 'HUMAN';
        return `<div class="${role}"><b>${label}:</b> ${text}</div>`;
      }).join('')
    : '<i style="color:#444">No transcript</i>';
  td.appendChild(box); row.appendChild(td); tr.after(row);
}
load();
</script></body></html>"""


def serialise(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


async def fetch_calls():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    try:
        rows = await conn.fetch(
            """
            SELECT conversation_id, caller_number, contractor_name,
                   shipment_id, shipment_status, follow_up_date,
                   human_callback, duration_secs, transcript, ended_at
            FROM elevenlabs_calls
            ORDER BY ended_at DESC NULLS LAST
            LIMIT 100
            """
        )
    finally:
        await conn.close()
    result = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("transcript"), str):
            try:
                d["transcript"] = json.loads(d["transcript"])
            except Exception:
                d["transcript"] = []
        result.append(json.loads(json.dumps(d, default=serialise)))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == "/api/elevenlabs-calls":
            try:
                data = asyncio.run(fetch_calls())
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
        else:
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = 8081
    print(f"ElevenLabs dashboard running at http://localhost:{port}")
    HTTPServer(("", port), Handler).serve_forever()
