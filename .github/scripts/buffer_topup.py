"""Keeps ONE client's Buffer queue full without anyone's computer or chat: runs on GitHub's servers every 30 minutes.

    python .github/scripts/buffer_topup.py <client folder>

Each client has its own Buffer account and its own key. The workflow gives this run ONLY that client's key (env BUFFER_API_KEY, filled from the client's own
repository secret BUFFER_KEY_<CLIENT>) and the client's own account names (env OWN_HANDLES, not secret).
1. Lists the channels this key reaches and keeps only the client's own (OWN_HANDLES) → <folder>/posting/buffer_channels.json
   (ids and names only — never the key).
2. Reads <folder>/posting/queue.json (written by the marketing engine): posts {id, due_utc, platform, channel_id, caption, video_url,
   ai, status: planned|scheduled, buffer_id}. Sends every «planned» post that is still in the future to Buffer, oldest first, until
   Buffer says its limit is reached — and only to a channel that is the client's own. Saves what it scheduled back into queue.json.
No key yet → says so and stops (not an error). No queue.json → only step 1."""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.buffer.com"


def lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {x[1:] if isinstance(x, str) and x.startswith('~') else lit(x)}" for k, x in v.items()) + "}"
    return str(v)


def gql(q, key):
    req = urllib.request.Request(API, data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read()[:200].decode('utf-8', 'replace')}") from None
    if j.get("errors"):
        raise RuntimeError(json.dumps(j["errors"])[:300])
    return j["data"]


def _h(x):
    return str(x or "").strip().lower().lstrip("@")


def channels(key, own_handles, call=gql):
    """(own channels, names of channels that are not this client's)."""
    own, others = [], []
    for o in call("query { account { organizations { id name } } }", key)["account"]["organizations"]:
        for ch in call(f"query {{ channels(input: {{organizationId: {lit(o['id'])}}}) {{ id name displayName service isQueuePaused }} }}", key)["channels"]:
            names = {_h(ch.get("name")), _h(ch.get("displayName"))}
            if not own_handles or names & own_handles:
                own.append({"id": ch["id"], "service": str(ch.get("service", "")).lower(), "name": ch.get("displayName") or ch.get("name"),
                            "paused": bool(ch.get("isQueuePaused"))})
            else:
                others.append(ch.get("displayName") or ch.get("name"))
    return own, others


def run(folder, key, own_handles, now=None, call=gql):
    if not key:
        print(f"{folder}: no Buffer key for this client yet (secret not saved) — skipped")
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    own, others = channels(key, own_handles, call)
    os.makedirs(os.path.join(folder, "posting"), exist_ok=True)
    with open(os.path.join(folder, "posting", "buffer_channels.json"), "w", encoding="utf-8") as f:
        json.dump({"checked": now.isoformat(timespec="minutes"), "channels": own, "not_this_client": others}, f, ensure_ascii=False, indent=1)
    qf = os.path.join(folder, "posting", "queue.json")
    if not os.path.exists(qf):
        print(f"{folder}: channels checked ({len(own)} own); no queue yet")
        return 0
    ids = {c["id"] for c in own}
    q = json.load(open(qf, encoding="utf-8"))
    full, changed, bad = set(), False, 0
    for p in sorted(q["posts"], key=lambda p: p["due_utc"]):
        due = dt.datetime.fromisoformat(p["due_utc"].replace("Z", "+00:00"))
        if p["status"] != "planned" or p["platform"] in full:
            continue
        if p["channel_id"] not in ids:
            bad += 1
            print("REFUSED", qf, p["id"], p["platform"], "— not one of this client's own Buffer channels")
            continue
        if due < now + dt.timedelta(minutes=15):
            p["status"] = "missed"
            changed, bad = True, bad + 1
            print("MISSED", qf, p["id"], p["platform"])
            continue
        meta = ({"instagram": {"type": "~reel", "shouldShareToFeed": True, "isAiGenerated": bool(p.get("ai"))}} if p["platform"] == "instagram"
                else {"tiktok": {"isAiGenerated": bool(p.get("ai"))}})
        inp = {"text": p["caption"], "channelId": p["channel_id"], "schedulingType": "~automatic", "mode": "~customScheduled",
               "dueAt": due.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "assets": [{"video": {"url": p["video_url"]}}], "metadata": meta}
        res = call(f"mutation {{ createPost(input: {lit(inp)}) {{ ... on PostActionSuccess {{ post {{ id }} }} ... on MutationError {{ message }} }} }}", key)["createPost"]
        if res.get("post"):
            p["status"], p["buffer_id"], changed = "scheduled", res["post"]["id"], True
            print("scheduled", qf, p["id"], p["platform"])
        elif "limit" in str(res.get("message", "")).lower():
            full.add(p["platform"])
        else:
            bad += 1
            print("REFUSED", qf, p["id"], p["platform"], res.get("message"))
    if changed:
        json.dump(q, open(qf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    hs = {_h(x) for x in os.environ.get("OWN_HANDLES", "").split(",") if _h(x)}
    sys.exit(run(sys.argv[1], os.environ.get("BUFFER_API_KEY", ""), hs))
