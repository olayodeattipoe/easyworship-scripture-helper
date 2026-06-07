import { useState, useEffect, useRef } from "react";
import { io } from "socket.io-client";
import { Mic, MicOff, BookOpen, Clock, Wifi, WifiOff, Download, ChevronRight } from "lucide-react";
import "./App.css";

const SOCKET_URL = "http://localhost:5000";
const VERSIONS   = ["NKJV", "NIV", "KJV"];

function VerseCard({ item, activeVer, setActiveVer }) {
  const paths = item.cached || {};

  const handleDragStart = (e, version) => {
    const filename = paths[version]?.split(/[\\/]/).pop();
    if (!filename) return;
    e.dataTransfer.setData("text/plain", `${SOCKET_URL}/pptx/${filename}`);
    e.dataTransfer.setData("DownloadURL", `application/octet-stream:${filename}:${SOCKET_URL}/pptx/${filename}`);
  };

  const formatRef = (id) => {
    if (!id) return "—";
    const [book, ch] = id.split(".");
    return ch ? `${book} ${ch}` : book;
  };

  return (
    <div className={`verse-card ${item.complete ? "complete" : "loading"}`}>
      <div className="verse-card-header">
        <span className="verse-ref">{formatRef(item.passage_id)}</span>
        {!item.complete && <span className="badge loading-badge">fetching…</span>}
        {item.complete  && <span className="badge ready-badge">ready</span>}
      </div>

      <div className="version-tabs">
        {VERSIONS.map(v => (
          <button
            key={v}
            className={`ver-tab ${activeVer === v ? "active" : ""} ${paths[v] ? "has-file" : "no-file"}`}
            onClick={() => paths[v] && setActiveVer(v)}
          >
            {v}
          </button>
        ))}
      </div>

      {VERSIONS.map(v => {
        const filename = paths[v]?.split(/[\\/]/).pop();
        if (activeVer !== v || !filename) return null;
        return (
          <div
            key={v}
            className="drag-zone"
            draggable
            onDragStart={e => handleDragStart(e, v)}
          >
            <div className="drag-icon"><Download size={14} /></div>
            <div className="drag-text">
              <span className="drag-label">drag into EasyWorship</span>
              <span className="drag-hint">{filename}</span>
            </div>
            <ChevronRight size={13} className="drag-arrow" />
          </div>
        );
      })}
    </div>
  );
}

function TranscriptFeed({ lines, interim }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines, interim]);

  return (
    <div className="transcript-feed" ref={ref}>
      {lines.length === 0 && !interim && (
        <span className="empty-hint">transcript will appear here…</span>
      )}
      {lines.map((l, i) => (
        <p key={i} className="transcript-line">{l}</p>
      ))}
      {interim && <p className="transcript-interim">{interim}</p>}
    </div>
  );
}

export default function App() {
  const [connected,  setConnected]  = useState(false);
  const [items,      setItems]      = useState([]);
  const [activeVers, setActiveVers] = useState({});
  const [lines,      setLines]      = useState([]);
  const [interim,    setInterim]    = useState("");
  const socketRef = useRef(null);

  useEffect(() => {
    const s = io(SOCKET_URL, { transports: ["websocket"] });
    socketRef.current = s;

    s.on("connect",    () => setConnected(true));
    s.on("disconnect", () => setConnected(false));
    s.on("status", ({ connected: c }) => setConnected(c));

    s.on("transcript", ({ text }) => {
      setLines(prev => [...prev.slice(-80), text]);
      setInterim("");
    });

    s.on("transcript_interim", ({ text }) => setInterim(text));

    s.on("scripture_ready", (data) => {
      setItems(prev => {
        const idx = prev.findIndex(x => x.passage_id === data.passage_id);
        if (idx === -1) return [data, ...prev];
        const updated = [...prev];
        updated[idx]  = data;
        return updated;
      });
      setActiveVers(prev => ({
        ...prev,
        [data.passage_id]: prev[data.passage_id] || "NKJV",
      }));
    });

    return () => s.disconnect();
  }, []);

  const setVerFor = (pid, ver) =>
    setActiveVers(prev => ({ ...prev, [pid]: ver }));

  return (
    <div className="panel">

      {/* Header */}
      <div className="panel-header">
        <div className="logo">
          <BookOpen size={15} />
          <span>EW Helper</span>
        </div>
        <div className={`conn-pill ${connected ? "online" : "offline"}`}>
          {connected ? <Wifi size={11} /> : <WifiOff size={11} />}
          <span>{connected ? "live" : "offline"}</span>
        </div>
      </div>

      {/* Mic bar */}
      <div className={`mic-bar ${connected ? "active" : ""}`}>
        <div className="mic-icon">
          {connected ? <Mic size={13} /> : <MicOff size={13} />}
        </div>
        <span className="mic-label">
          {connected ? "listening to pastor" : "waiting for backend…"}
        </span>
        {connected && (
          <div className="pulse-dots"><span/><span/><span/></div>
        )}
      </div>

      {/* Two-column body */}
      <div className="panel-body">

        {/* Left — scriptures */}
        <div className="panel-col">
          <div className="col-label">
            <Clock size={11} />
            <span>detected &nbsp;<em>{items.length}</em></span>
          </div>
          <div className="cards-list">
            {items.length === 0 && (
              <div className="empty-card">scripture references will appear here</div>
            )}
            {items.map(item => (
              <VerseCard
                key={item.passage_id}
                item={item}
                activeVer={activeVers[item.passage_id] || "NKJV"}
                setActiveVer={ver => setVerFor(item.passage_id, ver)}
              />
            ))}
          </div>
        </div>

        {/* Right — transcript */}
        <div className="panel-col">
          <div className="col-label">
            <Mic size={11} />
            <span>transcript</span>
          </div>
          <TranscriptFeed lines={lines} interim={interim} />
        </div>

      </div>
    </div>
  );
}

