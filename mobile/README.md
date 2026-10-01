# Canary Observability — Mobile Client (React Native)

A cross-platform React Native companion app for the **Observability Canary Dashboard**, designed to monitor real-time telemetry, manage polyglot microservice power/lifecycle, trigger chaos drills, inspect infrastructure dependencies, and run PostgreSQL queries from **Android**, **iOS**, and **Windows** devices.

---

## 📱 Features

1. **📊 Live Telemetry KPIs**:
   - Availability %, total synthetic requests, error count, and Canary throughput (TPS).
   - Average latency, P50, P95, and P99 latency distribution.
   - Real-time traffic stream with method, path, latency, and status badges.
2. **🎯 Polyglot Microservices & Power Management**:
   - Monitor all 7 language microservices: Python, Java, Rust, Node.js, Go, C# .NET, and C.
   - Remote Container Power Control: Shut down and restart unique language containers directly from mobile to save system resources. The Canary workload automatically suspends requests to stopped containers, preventing artificial failure spikes.
3. **⚡ Chaos & Fault Injection Testing**:
   - Trigger language-targeted error spikes, latency surges, service outages, or intermittent error storms.
   - Trigger targeted process crashes or worker thread crashes directly from your device.
   - Instantly abort active drills with a single tap.
4. **🔌 Stack Dependencies Health**:
   - Comprehensive roll-up of all 9 backend infrastructure services:
     - **PostgreSQL** (Relational & Audit Store)
     - **Valkey** (In-Memory Telemetry Cache)
     - **OpenTelemetry Collector** (OTLP gRPC Pipeline)
     - **Grafana Tempo** (Distributed Tracing Backend)
     - **Grafana Loki** (Log Aggregator)
     - **Grafana Mimir** (Prometheus Long-Term Metrics Store)
     - **Prometheus** (Metrics Scraper)
     - **Grafana** (Visualization UI)
     - **Redpanda** (Kafka-compatible Event Streaming)
   - Real-time latency, availability %, and capacity metrics.
5. **💾 PostgreSQL SQL Query Runner**:
   - Curated saved queries for instant execution.
   - Custom query editor with execution timing and row count.
   - First-principles tabular data grid with column sorting and NULL value handling.

---

## 🚀 Quick Start

### 1. Connect via Dashboard QR Code
1. Open the Canary Dashboard in your desktop browser: `http://localhost:8085/`.
2. Click **📱 Connect / QR** in the top header.
3. If connecting from a physical phone on the same Wi-Fi network, enter your machine's local LAN IP (e.g. `192.168.1.100`) into the modal input field.
4. Scan the dynamically rendered QR code with your phone camera or the Expo Go mobile app.

---

### 2. Running Locally

#### Prerequisites
- Node.js (v18+)
- npm or yarn

```bash
cd mobile
npm install
```

#### Run on Web (Instant Preview)
```bash
npm run web
```

#### Run on Android Device / Emulator
```bash
# If using Android emulator, the default host is http://10.0.2.2:8085
npm run android
```

#### Run on iOS Simulator (macOS)
```bash
npm run ios
```

#### Run on Windows Desktop
```bash
npm run windows
```

---

## ⚙️ Configuration

In the top right corner of the mobile app, tap **⚙️ URL** to change the Canary server endpoint:
- **Localhost (desktop / web)**: `http://localhost:8085`
- **Android Emulator**: `http://10.0.2.2:8085`
- **Physical Device (Wi-Fi)**: `http://<YOUR_COMPUTER_LAN_IP>:8085`
