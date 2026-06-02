"""
Run locally:  python dashboard.py
Then open:    http://localhost:8080
Connects to the DB via DATABASE_URL in your .env
"""

import asyncio, asyncpg, json, os
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Calls</title>
<style>
  body { font-family: monospace; background: #111; color: #ddd; padding: 20px; }
  button { background: #2563eb; color: #fff; border: none; padding: 8px 18px; cursor: pointer; border-radius: 4px; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 12px; }
  th { background: #1a1a1a; color: #888; text-align: left; padding: 8px; border-bottom: 1px solid #333; }
  td { padding: 8px; border-bottom: 1px solid #1e1e1e; vertical-align: top; }
  tr:hover td { background: #1a1a1a; cursor: pointer; }
  .transcript { background: #0d0d0d; padding: 10px; font-size: 11px; line-height: 1.8; }
  .user { color: #60a5fa; } .assistant { color: #a78bfa; }
  #info { font-size: 12px; color: #555; margin-left: 10px; }
  .badge { display:inline-block; padding:2px 7px; border-radius:3px; font-size:11px; font-weight:bold; }
  .badge-yes { background:#7f1d1d; color:#fca5a5; }
  .badge-no  { background:#14532d; color:#86efac; }
  .badge-scheduled  { background:#1e3a5f; color:#93c5fd; }
  .badge-ended      { background:#2d2d2d; color:#888; }
</style></head>
<body>
<h2 style="margin-bottom:12px">Shipment Follow-up Calls</h2>
<button onclick="load()">Refresh</button><span id="info"></span>
<table>
  <thead><tr>
    <th>#</th>
    <th>Caller</th>
    <th>Shipment ID</th>
    <th>Shipment Status</th>
    <th>Follow-up Date</th>
    <th>Human Callback</th>
    <th>Outcome</th>
    <th>Cost</th>
    <th>Ended</th>
  </tr></thead>
  <tbody id="tb"></tbody>
</table>
<script>
function badge(text, cls) {
  return `<span class="badge ${cls}">${text}</span>`;
}
function outcomeBadge(o) {
  if (!o) return '—';
  if (o === 'follow_up_scheduled')     return badge('scheduled', 'badge-scheduled');
  if (o === 'human_callback_requested') return badge('human req.', 'badge-yes');
  if (o === 'wrong_person')             return badge('wrong #', 'badge-ended');
  return badge(o, 'badge-ended');
}
async function load() {
  document.getElementById('info').textContent = 'Loading...';
  let rows;
  try {
    const res = await fetch('/api/calls');
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
    const tr = document.createElement('tr');
    const humanBadge = c.wants_human ? badge('Yes', 'badge-yes') : badge('No', 'badge-no');
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${c.caller_number||'—'}</td>
      <td style="max-width:140px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${c.purpose||'—'}</td>
      <td style="max-width:180px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${c.shipment_status||'—'}</td>
      <td>${c.follow_up_time||'—'}</td>
      <td>${humanBadge}</td>
      <td>${outcomeBadge(c.outcome)}</td>
      <td>${c.cost!=null?'$'+Number(c.cost).toFixed(4):'—'}</td>
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
    ? items.map(m => `<div class="${m.role}"><b>${m.role==='user'?'CALLER':'AL'}:</b> ${m.text||'<i style="color:#444">audio only</i>'}</div>`).join('')
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
            "SELECT caller_number, direction, purpose, follow_up_time, "
            "shipment_status, wants_human, outcome, cost, ended_at, transcript "
            "FROM calls ORDER BY ended_at DESC NULLS LAST LIMIT 100"
        )
    finally:
        await conn.close()
    result = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("transcript"), str):
            try: d["transcript"] = json.loads(d["transcript"])
            except: d["transcript"] = []
        result.append(json.loads(json.dumps(d, default=serialise)))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        if self.path == "/api/calls":
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
    port = 8080
    print(f"Dashboard running at http://localhost:{port}")
    HTTPServer(("", port), Handler).serve_forever()
