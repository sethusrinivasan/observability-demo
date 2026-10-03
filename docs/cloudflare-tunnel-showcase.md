# 🌐 Cloudflare Tunnel Live Showcase Guide

A comprehensive architectural guide for hosting the **Observability Demo & Canary Operations Platform** as a 24/7 live public showcase from a dedicated home server connected to a residential broadband connection from your internet service provider.

---

## 1. Why Cloudflare Tunnel is the Gold Standard

Hosting a public showcase on residential broadband typically presents several friction points:
* **Dynamic IP Addresses**: Home ISP leases rotate periodically, breaking static DNS mappings.
* **ISP Inbound Port Filtering**: Many residential internet service providers filter or flag unsolicited inbound HTTP (`port 80`) traffic or block SMTP (`port 25`).
* **Router & Gateway Fragility**: Modern ISP routers and gateways often manage port forwarding via proprietary cloud apps that can be cumbersome and prone to state resets.
* **Security & DDoS Exposure**: Port forwarding exposes your residential public IP address to port scanners and malicious probes on the open internet.

**Cloudflare Tunnel (`cloudflared`)** solves all of these problems through **Zero-Port-Forwarding**:

```mermaid
flowchart LR
    subgraph Internet["Public Internet (Mobile, Laptop, Evaluator)"]
        User["Visitor / Evaluator<br/>(Anywhere in the World)"]
    end

    subgraph Cloudflare["Cloudflare Global Anycast Edge"]
        CF_Edge["Cloudflare Edge Proxy<br/>Automatic TLS (HTTPS)<br/>DDoS & WAF Protection"]
    end

    subgraph Home_Network["Home Network (LAN)"]
        Gateway["Home Router / Gateway<br/>(ALL INBOUND PORTS CLOSED)"]
        
        subgraph Dedicated_Host["Dedicated Home Machine (Ubuntu 24.04 LTS)"]
            Cloudflared["cloudflared Container<br/>(Outbound TLS Tunnel Only)"]
            
            subgraph Docker_Network["Docker: observability-network"]
                Canary["Canary Dashboard<br/>http://canary:8085"]
                Grafana["Grafana LGTM<br/>http://grafana:3000"]
                Polyglot["7 Microservices<br/>(Java, Python, Rust, etc.)"]
            end
        end
    end

    User -->|HTTPS| CF_Edge
    Cloudflared -.->|Outbound QUIC/TLS Tunnel (Ports 7844/443)| CF_Edge
    CF_Edge -->|Multiplexed Stream| Cloudflared
    Cloudflared -->|HTTP| Canary
    Cloudflared -->|HTTP| Grafana
    Canary --> Polyglot
```

### Key Advantages
1. **Zero Open Inbound Ports**: The `cloudflared` daemon opens an *outbound-only* multiplexed connection (over QUIC/HTTP2) to Cloudflare's nearest edge data center. Your home router firewall remains 100% closed.
2. **Automated SSL/TLS**: Cloudflare provisions and auto-renews valid TLS certificates.
3. **Immune to Inbound Port Filtering**: Because connections are established outbound, your internet service provider's inbound port blocks, CGNAT, and security filters never interfere with traffic.
4. **IP Obfuscation**: Visitors only ever see Cloudflare edge IPs; your home public IP address is never revealed.

---

## 2. Option A: Instant Quick Tunnel (TryCloudflare)

If you do not have a custom domain or Cloudflare account yet, you can spin up an ephemeral public HTTPS URL in under 5 seconds:

```bash
./scripts/showcase_tunnel.sh --quick
```

**Output:**
```text
=================================================================
 🎉 LIVE PUBLIC SHOWCASE URL READY!
=================================================================

  Public HTTPS URL:  https://example-random-subdomain.trycloudflare.com
  Target Service:    Canary Telemetry & Load Dashboard (port 8085)
```

* **Instant Access**: Share this URL with anyone on mobile or desktop anywhere in the world.
* **Portfolio link**: `--quick` commits that URL into the observability-demo Live Demo link in `~/sethusrinivasan.github.io` and pushes it, so [sethusrinivasan.github.io](https://sethusrinivasan.github.io/) follows the current tunnel. Set `SHOWCASE_PAGES_REPO` if that checkout lives somewhere else.
* **Auto-Discovery**: The Canary dashboard's QR code modal automatically detects the tunnel hostname so phone scans open the secure URL.
* **Check Status**: `./scripts/showcase_tunnel.sh --status`
* **Stop Tunnel**: `./scripts/showcase_tunnel.sh --stop`

---

## 3. Option B: Permanent Named Showcase Tunnel (Cloudflare Zero Trust)

For a permanent, branded showcase (e.g. `showcase.yourdomain.com` and `grafana.yourdomain.com`), use Cloudflare's **free** Zero Trust tier.

### Step 1: Create a Tunnel in Cloudflare
1. Log in to the [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Navigate to **Zero Trust** -> **Networks** -> **Tunnels**.
3. Click **Add a tunnel** -> Select **Cloudflared**.
4. Name your tunnel (e.g. `observability-home-showcase`) and click **Save tunnel**.
5. Under **Install and run a connector**, select **Docker** and copy the tunnel token (the long base64 string after `--token`).

### Step 2: Configure Public Hostnames in Cloudflare
Under the **Public Hostnames** tab of your tunnel, add two routes:

| Public Hostname | Service Type | URL | Notes |
| :--- | :--- | :--- | :--- |
| `showcase.yourdomain.com` | `HTTP` | `canary:8085` | Canary Dashboard & Traffic Generator |
| `grafana.yourdomain.com` | `HTTP` | `grafana:3000` | Grafana LGTM Observability Stack |

*(Notice: Because `cloudflared` runs inside Docker on `observability-network`, it addresses `canary:8085` and `grafana:3000` directly by container name without host port mapping!)*

### Step 3: Launch with Your Token
Run with the token directly:
```bash
./scripts/showcase_tunnel.sh --token <YOUR_CLOUDFLARE_TUNNEL_TOKEN>
```

Or store it in your `.env` file for persistent reboots:
```bash
echo "CLOUDFLARE_TUNNEL_TOKEN=<YOUR_CLOUDFLARE_TUNNEL_TOKEN>" >> .env
docker compose --profile tunnel up -d cloudflared
```

---

## 4. Dedicated Home Machine 24/7 Resilience Checklist

To ensure your dedicated home server runs unattended as a 24/7 showcase, configure these four layers:

### Layer 1: Motherboard BIOS AC Power Recovery
If a power flicker occurs, consumer motherboards typically stay powered off.
1. Enter your BIOS/UEFI during boot (press `DEL` or `F2`).
2. Locate **Power Management** / **ACPI Settings**.
3. Change **Restore on AC Power Loss** (or *State After G3*) from `Power Off` to **`Power On`** (or `Always On`).
4. Save and exit. The machine will now boot automatically whenever power is restored.

### Layer 2: Static DHCP Lease in Your Home Router / Gateway
Ensure your home machine retains the same internal IP address (e.g., `192.168.86.35`):
1. Open your router's administration interface or mobile management app.
2. Select your dedicated server -> **Device Details**.
3. Assign a **Reserved / Static IP Address**.

### Layer 3: Systemd Auto-Start for Docker Compose
Create a systemd unit so the entire observability stack and tunnel launch on boot without needing a user login:

```ini
# /etc/systemd/system/observability-showcase.service
[Unit]
Description=Observability Demo 24/7 Showcase Stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/home/github/observability-demo
ExecStart=/usr/bin/docker compose --profile tunnel up -d
ExecStop=/usr/bin/docker compose --profile tunnel stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Enable and activate the unit:
```bash
sudo systemctl daemon-reload
sudo systemctl enable observability-showcase.service
```

### Layer 4: Host OS Firewall Hardening (UFW)
Ensure local services are protected while keeping SSH and local management open:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh comment 'SSH Administration'
sudo ufw allow from 192.168.0.0/16 to any port 8085 proto tcp comment 'Canary LAN Access'
sudo ufw allow from 192.168.0.0/16 to any port 3000 proto tcp comment 'Grafana LAN Access'
sudo ufw enable
```

---

## 5. Summary Matrix

| Feature | Direct Port Forwarding | Dynamic DNS (DDNS) | Cloudflare Tunnel (This Solution) |
| :--- | :---: | :---: | :---: |
| **Open Router Ports** | ⚠️ Required (80, 443, 8085) | ⚠️ Required | ✅ **0 Open Ports** |
| **Bypasses ISP Filtering** | ❌ Blocked/Filtered | ❌ Blocked/Filtered | ✅ **100% Bypassed** |
| **Automated Free HTTPS** | ⚠️ Complex (Certbot/Cron) | ⚠️ Complex | ✅ **Native & Automatic** |
| **DDoS & Scraping Protection** | ❌ None | ❌ None | ✅ **Cloudflare Edge WAF** |
| **Works on CGNAT / Hotspots** | ❌ No | ❌ No | ✅ **Yes** |
| **Custom Domain Support** | ⚠️ Complex | ⚠️ Limited | ✅ **Full DNS / Subdomains** |
