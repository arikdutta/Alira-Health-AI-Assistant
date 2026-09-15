// src/AliraDashboard.jsx
import React, { useState } from 'react';

// Every /query answer is {type, message, data}; type says which engine answered
const RESULT_TITLES = {
  MA_TARGETS: "M&A Target Screening",
  MARKET_ACCESS: "Market Access",
  RWE_SEARCH: "Real-World Evidence Registries",
};

const cellStyle = { padding: '12px 15px', color: '#334155' };
const headerCellStyle = { padding: '12px 15px', color: '#475569' };
const tableFrameStyle = { overflowX: 'auto', border: '1px solid #e2e8f0', borderRadius: '8px', boxShadow: '0 1px 3px rgba(0,0,0,0.05)' };
const tableStyle = { width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '14px' };

function formatCell(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toLocaleString();
  return String(value);
}

function TargetsTable({ rows }) {
  return (
    <div style={tableFrameStyle}>
      <table style={tableStyle}>
        <thead style={{ backgroundColor: '#f8fafc', borderBottom: '1px solid #e2e8f0' }}>
          <tr>
            <th style={headerCellStyle}>Company Target</th>
            <th style={headerCellStyle}>Therapeutic Field</th>
            <th style={headerCellStyle}>Country</th>
            <th style={headerCellStyle}>Annual Gross Revenue</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} style={{ borderBottom: '1px solid #f1f5f9' }}>
              <td style={{ ...cellStyle, fontWeight: '600', color: '#0f172a' }}>{row.COMPANY_NAME}</td>
              <td style={cellStyle}>{row.THERAPEUTIC_AREA}</td>
              <td style={cellStyle}>{row.COUNTRY}</td>
              <td style={{ ...cellStyle, fontFamily: 'monospace', color: '#047857', fontWeight: 'bold' }}>
                ${row.REVENUE?.toLocaleString(undefined, { minimumFractionDigits: 2 })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Agent tool results vary by tool (Cortex Search hits with scores, SQL result sets), so columns come from the rows
function RecordsTable({ rows }) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  return (
    <div style={tableFrameStyle}>
      <table style={tableStyle}>
        <thead style={{ backgroundColor: '#f8fafc', borderBottom: '1px solid #e2e8f0' }}>
          <tr>
            {columns.map((column) => <th key={column} style={headerCellStyle}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} style={{ borderBottom: '1px solid #f1f5f9' }}>
              {columns.map((column) => <td key={column} style={cellStyle}>{formatCell(row[column])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AliraDashboard() {
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // The prompt and trace id of the last answer, so a consultant can flag it as wrong
  const [lastAnswer, setLastAnswer] = useState(null);
  const [reportStatus, setReportStatus] = useState("");

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!prompt.trim()) return;

    setLoading(true);
    setError("");
    setResult(null);
    setLastAnswer(null);
    setReportStatus("");

    try {
      // Connects directly to your local FastAPI gateway app instance
      const response = await fetch('http://localhost:8000/query', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ prompt: prompt }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Server failed to process query.");
      }

      const data = await response.json();
      const traceId = response.headers.get('X-Trace-Id');
      if (traceId) setLastAnswer({ traceId, prompt });

      // The engine couldn't answer (no matching practice, unclear request, ...)
      if (data.type === "GENERIC_MESSAGE") {
        setError(data.message);
      } else {
        setResult(data);
      }
    } catch (err) {
      setError(err.message || "An unexpected network error occurred.");
    } finally {
      setLoading(false);
    }
  };

  const reportWrongResult = async () => {
    setReportStatus("sending");
    try {
      const response = await fetch('http://localhost:8000/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trace_id: lastAnswer.traceId, prompt: lastAnswer.prompt }),
      });
      setReportStatus(response.ok ? "sent" : "failed");
    } catch {
      setReportStatus("failed");
    }
  };

  return (
    <div style={{ fontFamily: 'Arial, sans-serif', padding: '30px', maxWidth: '900px', margin: '0 auto' }}>
      <header style={{ marginBottom: '30px', borderBottom: '2px solid #eaeaea', paddingBottom: '15px' }}>
        <h1 style={{ color: '#1e293b', margin: '0 0 5px 0' }}>🔬 Alira Market Intelligence Hub</h1>
        <p style={{ color: '#64748b', margin: 0, fontSize: '14px' }}>Personal Sandbox Project • Connected to Snowflake, Azure CLU & Foundry Agents</p>
      </header>

      {/* Query Search Form Area */}
      <form onSubmit={handleSearch} style={{ display: 'flex', gap: '10px', marginBottom: '25px' }}>
        <input
          type="text"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="e.g., Show me oncology targets in Germany with revenue over 20M"
          style={{
            flex: 1,
            padding: '12px',
            borderRadius: '6px',
            border: '1px solid #cbd5e1',
            fontSize: '14px',
            outline: 'none'
          }}
          disabled={loading}
        />
        <button
          type="submit"
          style={{
            backgroundColor: loading ? '#94a3b8' : '#2563eb',
            color: 'white',
            border: 'none',
            padding: '12px 24px',
            borderRadius: '6px',
            fontWeight: 'bold',
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: '14px'
          }}
          disabled={loading}
        >
          {loading ? "Querying Engine..." : "Analyze"}
        </button>
      </form>

      {/* Conditional Rendering Notification Panel */}
      {error && (
        <div style={{ backgroundColor: '#fef2f2', border: '1px solid #fca5a5', color: '#991b1b', padding: '15px', borderRadius: '6px', marginBottom: '20px', fontSize: '14px' }}>
          ⚠️ {error}
        </div>
      )}

      {!result && !error && (
        <div style={{ ...tableFrameStyle, textAlign: 'center', padding: '40px', color: '#94a3b8', fontStyle: 'italic', fontSize: '14px' }}>
          No active query calculations executed. Submit an analyst prompt above.
        </div>
      )}

      {result && (
        <section>
          <h2 style={{ color: '#1e293b', fontSize: '16px', margin: '0 0 12px 0' }}>{RESULT_TITLES[result.type] ?? "Results"}</h2>
          {result.message && (
            // Agent answers are prose and can span paragraphs
            <div style={{ backgroundColor: '#f8fafc', border: '1px solid #e2e8f0', color: '#1e293b', padding: '15px', borderRadius: '6px', marginBottom: '16px', fontSize: '14px', whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
              {result.message}
            </div>
          )}
          {result.data.length > 0 && (
            result.type === "MA_TARGETS" ? <TargetsTable rows={result.data} /> : <RecordsTable rows={result.data} />
          )}
        </section>
      )}

      {lastAnswer && (
        <div style={{ marginTop: '12px', textAlign: 'right', fontSize: '13px', color: '#64748b' }}>
          {reportStatus === "sent" ? (
            "Thanks, the data steward will review this answer."
          ) : (
            <>
              {reportStatus === "failed" && <span style={{ color: '#991b1b', marginRight: '10px' }}>Couldn't send the report. Try again.</span>}
              <button
                type="button"
                onClick={reportWrongResult}
                disabled={reportStatus === "sending"}
                style={{ background: 'none', border: 'none', color: '#2563eb', cursor: 'pointer', fontSize: '13px', textDecoration: 'underline', padding: 0 }}
              >
                {reportStatus === "sending" ? "Sending..." : "Report wrong result"}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
