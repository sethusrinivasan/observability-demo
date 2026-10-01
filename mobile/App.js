import React, { useState, useEffect, useRef } from 'react';
import {
  StyleSheet,
  Text,
  View,
  ScrollView,
  TouchableOpacity,
  TextInput,
  Modal,
  Alert,
  SafeAreaView,
  StatusBar,
  ActivityIndicator,
  Platform,
} from 'react-native';

const DEFAULT_SERVER_URL = Platform.OS === 'android' ? 'http://10.0.2.2:8085' : 'http://localhost:8085';

export default function App() {
  const [serverUrl, setServerUrl] = useState(DEFAULT_SERVER_URL);
  const [inputUrl, setInputUrl] = useState(DEFAULT_SERVER_URL);
  const [connected, setConnected] = useState(false);
  const [activeTab, setActiveTab] = useState('overview'); // overview, services, chaos, dependencies, sql
  const [stats, setStats] = useState(null);
  const [dependencies, setDependencies] = useState(null);
  const [containers, setContainers] = useState(null);
  const [savedQueries, setSavedQueries] = useState([]);
  const [sqlQuery, setSqlQuery] = useState('SELECT id, created_at, endpoint, status_code, coalesce(details->>\'language\', \'python\') AS language FROM audit_logs ORDER BY id DESC LIMIT 20;');
  const [sqlResult, setSqlResult] = useState(null);
  const [sqlLoading, setSqlLoading] = useState(false);
  const [settingsVisible, setSettingsVisible] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // Periodic poll
  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 3000);
    return () => clearInterval(interval);
  }, [serverUrl]);

  async function fetchData() {
    try {
      const statsRes = await fetch(`${serverUrl}/stats`, { headers: { 'Accept': 'application/json' } });
      if (statsRes.ok) {
        const statsJson = await statsRes.json();
        setStats(statsJson);
        setConnected(true);
      } else {
        setConnected(false);
      }

      // Fetch container states
      try {
        const cRes = await fetch(`${serverUrl}/api/containers`);
        if (cRes.ok) {
          const cJson = await cRes.json();
          setContainers(cJson);
        }
      } catch (_) {}

    } catch (e) {
      setConnected(false);
    }
  }

  async function fetchDeps() {
    try {
      setRefreshing(true);
      const res = await fetch(`${serverUrl}/api/dependencies`);
      if (res.ok) {
        const json = await res.json();
        setDependencies(json);
      }
    } catch (e) {
      Alert.alert('Error', 'Failed to fetch dependencies: ' + e.message);
    } finally {
      setRefreshing(false);
    }
  }

  async function fetchSavedQueries() {
    try {
      const res = await fetch(`${serverUrl}/api/sql/saved-queries`);
      if (res.ok) {
        const json = await res.json();
        setSavedQueries(json);
      }
    } catch (_) {}
  }

  useEffect(() => {
    if (activeTab === 'dependencies') {
      fetchDeps();
    } else if (activeTab === 'sql') {
      fetchSavedQueries();
    }
  }, [activeTab]);

  async function executeSql() {
    if (!sqlQuery.trim()) return;
    setSqlLoading(true);
    try {
      const res = await fetch(`${serverUrl}/api/sql/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: sqlQuery }),
      });
      const json = await res.json();
      setSqlResult(json);
    } catch (e) {
      Alert.alert('SQL Execution Failed', e.message);
    } finally {
      setSqlLoading(false);
    }
  }

  async function toggleContainer(lang, action) {
    try {
      const res = await fetch(`${serverUrl}/api/containers/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: lang, action: action }),
      });
      if (res.ok) {
        Alert.alert('Success', `Container ${lang.toUpperCase()} ${action}ed`);
        fetchData();
      } else {
        const err = await res.json();
        Alert.alert('Error', err.message || 'Operation failed');
      }
    } catch (e) {
      Alert.alert('Error', e.message);
    }
  }

  async function triggerFault(tag, type, target, duration = 60) {
    try {
      const res = await fetch(`${serverUrl}/api/fault/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag, fault_type: type, target, duration_sec: duration }),
      });
      if (res.ok) {
        Alert.alert('Fault Drill Started', `Active drill: ${tag}`);
        fetchData();
      }
    } catch (e) {
      Alert.alert('Error', e.message);
    }
  }

  async function triggerCrash(target, type) {
    try {
      const res = await fetch(`${serverUrl}/api/crash`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, type, tag: `MOBILE-CRASH-${target.toUpperCase()}` }),
      });
      if (res.ok) {
        Alert.alert('Crash Triggered', `Issued ${type} crash to ${target.toUpperCase()}`);
        fetchData();
      }
    } catch (e) {
      Alert.alert('Error', e.message);
    }
  }

  async function stopFault() {
    try {
      await fetch(`${serverUrl}/api/fault/stop`, { method: 'POST' });
      fetchData();
    } catch (_) {}
  }

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#090e17" />

      {/* Top Header */}
      <View style={styles.header}>
        <View style={styles.titleRow}>
          <View style={[styles.statusDot, { backgroundColor: connected ? '#10b981' : '#ef4444' }]} />
          <Text style={styles.headerTitle}>Canary Mobile Monitor</Text>
        </View>
        <TouchableOpacity style={styles.settingsBtn} onPress={() => setSettingsVisible(true)}>
          <Text style={styles.settingsBtnText}>⚙️ URL</Text>
        </TouchableOpacity>
      </View>

      {/* Server Banner */}
      <View style={styles.serverBanner}>
        <Text style={styles.serverUrlText} numberOfLines={1}>{serverUrl}</Text>
        <Text style={[styles.connPill, { backgroundColor: connected ? '#064e3b' : '#7f1d1d', color: connected ? '#34d399' : '#f87171' }]}>
          {connected ? 'CONNECTED' : 'DISCONNECTED'}
        </Text>
      </View>

      {/* Navigation Tabs */}
      <View style={styles.navBar}>
        <TouchableOpacity style={[styles.navTab, activeTab === 'overview' && styles.activeNavTab]} onPress={() => setActiveTab('overview')}>
          <Text style={[styles.navTabText, activeTab === 'overview' && styles.activeNavTabText]}>📊 Overview</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.navTab, activeTab === 'services' && styles.activeNavTab]} onPress={() => setActiveTab('services')}>
          <Text style={[styles.navTabText, activeTab === 'services' && styles.activeNavTabText]}>🎯 Services</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.navTab, activeTab === 'chaos' && styles.activeNavTab]} onPress={() => setActiveTab('chaos')}>
          <Text style={[styles.navTabText, activeTab === 'chaos' && styles.activeNavTabText]}>⚡ Chaos</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.navTab, activeTab === 'dependencies' && styles.activeNavTab]} onPress={() => setActiveTab('dependencies')}>
          <Text style={[styles.navTabText, activeTab === 'dependencies' && styles.activeNavTabText]}>🔌 Deps</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.navTab, activeTab === 'sql' && styles.activeNavTab]} onPress={() => setActiveTab('sql')}>
          <Text style={[styles.navTabText, activeTab === 'sql' && styles.activeNavTabText]}>💾 SQL</Text>
        </TouchableOpacity>
      </View>

      {/* Main Content Area */}
      <ScrollView style={styles.content}>
        {/* Active Fault Alert Banner */}
        {stats?.fault_snapshot?.active_fault && (
          <View style={styles.faultAlertBanner}>
            <View style={{ flex: 1 }}>
              <Text style={styles.faultAlertTitle}>🚨 ACTIVE FAULT DRILL</Text>
              <Text style={styles.faultAlertSub}>
                [{stats.fault_snapshot.active_fault.target?.toUpperCase()}] {stats.fault_snapshot.active_fault.tag} ({stats.fault_snapshot.active_fault.fault_type})
              </Text>
            </View>
            <TouchableOpacity style={styles.abortBtn} onPress={stopFault}>
              <Text style={styles.abortBtnText}>Abort</Text>
            </TouchableOpacity>
          </View>
        )}

        {/* 1. OVERVIEW TAB */}
        {activeTab === 'overview' && (
          <View>
            {/* KPI Cards Grid */}
            <View style={styles.kpiGrid}>
              <View style={[styles.kpiCard, { borderTopColor: '#10b981' }]}>
                <Text style={styles.kpiLabel}>AVAILABILITY</Text>
                <Text style={[styles.kpiValue, { color: '#10b981' }]}>{stats?.availability_pct || 100}%</Text>
                <Text style={styles.kpiSub}>Success: {stats?.success_count || 0}</Text>
              </View>

              <View style={[styles.kpiCard, { borderTopColor: '#ef4444' }]}>
                <Text style={styles.kpiLabel}>TOTAL ERRORS</Text>
                <Text style={[styles.kpiValue, { color: '#ef4444' }]}>{stats?.error_count || 0}</Text>
                <Text style={styles.kpiSub}>Canary failures</Text>
              </View>

              <View style={[styles.kpiCard, { borderTopColor: '#38bdf8' }]}>
                <Text style={styles.kpiLabel}>SYNTHETIC REQS</Text>
                <Text style={styles.kpiValue}>{stats?.total_requests || 0}</Text>
                <Text style={styles.kpiSub}>Uptime: {Math.floor((stats?.uptime_seconds || 0) / 60)}m</Text>
              </View>

              <View style={[styles.kpiCard, { borderTopColor: '#a855f7' }]}>
                <Text style={styles.kpiLabel}>THROUGHPUT</Text>
                <Text style={[styles.kpiValue, { color: '#a855f7' }]}>{stats?.effective_tps || 6} TPS</Text>
                <Text style={styles.kpiSub}>Target: {stats?.base_tps || 6} TPS</Text>
              </View>
            </View>

            {/* Latency Card */}
            <View style={styles.card}>
              <Text style={styles.cardTitle}>Latency Profile</Text>
              <View style={styles.rowBetween}>
                <Text style={styles.label}>Average Latency:</Text>
                <Text style={styles.valMono}>{stats?.avg_latency_ms || 0} ms</Text>
              </View>
              <View style={styles.rowBetween}>
                <Text style={styles.label}>P50 Median:</Text>
                <Text style={styles.valMono}>{stats?.latency_p50_ms || 0} ms</Text>
              </View>
              <View style={styles.rowBetween}>
                <Text style={styles.label}>P95 Percentile:</Text>
                <Text style={styles.valMono}>{stats?.latency_p95_ms || 0} ms</Text>
              </View>
              <View style={styles.rowBetween}>
                <Text style={styles.label}>P99 Percentile:</Text>
                <Text style={styles.valMono}>{stats?.latency_p99_ms || 0} ms</Text>
              </View>
            </View>

            {/* Recent Requests Stream */}
            <View style={styles.card}>
              <Text style={styles.cardTitle}>Recent Traffic Stream</Text>
              {(stats?.recent_requests || []).slice(-8).reverse().map((r, i) => (
                <View key={i} style={styles.recentRow}>
                  <Text style={[styles.badge, styles[`badge_${r.lang}`]]}>{r.lang.toUpperCase()}</Text>
                  <Text style={styles.methodText}>{r.method}</Text>
                  <Text style={styles.pathText} numberOfLines={1}>{r.path}</Text>
                  <Text style={[styles.statusText, { color: r.status === 'success' ? '#10b981' : '#ef4444' }]}>
                    {r.status === 'success' ? 'OK' : 'ERR'}
                  </Text>
                  <Text style={styles.latText}>{r.duration_ms}ms</Text>
                </View>
              ))}
            </View>
          </View>
        )}

        {/* 2. MICROSERVICES TAB */}
        {activeTab === 'services' && (
          <View>
            <Text style={styles.sectionHeading}>Polyglot Microservices & Power Control</Text>
            {Object.entries(stats?.apps || {}).map(([lang, app]) => {
              const cInfo = containers?.[lang];
              const isRun = cInfo ? cInfo.is_running : true;
              return (
                <View key={lang} style={styles.serviceCard}>
                  <View style={styles.rowBetween}>
                    <View style={styles.rowAlignCenter}>
                      <Text style={[styles.badge, styles[`badge_${lang}`]]}>{lang.toUpperCase()}</Text>
                      <Text style={styles.serviceTitle}>{lang.toUpperCase()} App</Text>
                    </View>
                    <View style={styles.rowAlignCenter}>
                      <Text style={[styles.pill, { backgroundColor: isRun ? '#064e3b' : '#334155', color: isRun ? '#34d399' : '#94a3b8' }]}>
                        {isRun ? 'RUNNING' : 'STOPPED'}
                      </Text>
                    </View>
                  </View>

                  <View style={styles.serviceStatsRow}>
                    <View style={styles.statBox}>
                      <Text style={styles.statBoxLabel}>Availability</Text>
                      <Text style={[styles.statBoxVal, { color: app.availability_pct >= 99 ? '#10b981' : '#ef4444' }]}>
                        {app.availability_pct}%
                      </Text>
                    </View>
                    <View style={styles.statBox}>
                      <Text style={styles.statBoxLabel}>Requests</Text>
                      <Text style={styles.statBoxVal}>{app.total}</Text>
                    </View>
                    <View style={styles.statBox}>
                      <Text style={styles.statBoxLabel}>Errors</Text>
                      <Text style={[styles.statBoxVal, { color: '#ef4444' }]}>{app.error}</Text>
                    </View>
                    <View style={styles.statBox}>
                      <Text style={styles.statBoxLabel}>Avg Latency</Text>
                      <Text style={styles.statBoxVal}>{app.avg_latency_ms}ms</Text>
                    </View>
                  </View>

                  {/* Power Management Actions */}
                  <View style={styles.serviceActionRow}>
                    {isRun ? (
                      <TouchableOpacity style={[styles.actionBtn, styles.btnStop]} onPress={() => toggleContainer(lang, 'stop')}>
                        <Text style={styles.actionBtnText}>🛑 Stop Container</Text>
                      </TouchableOpacity>
                    ) : (
                      <TouchableOpacity style={[styles.actionBtn, styles.btnStart]} onPress={() => toggleContainer(lang, 'start')}>
                        <Text style={styles.actionBtnText}>▶ Start Container</Text>
                      </TouchableOpacity>
                    )}
                    <TouchableOpacity style={[styles.actionBtn, styles.btnRestart]} onPress={() => toggleContainer(lang, 'restart')}>
                      <Text style={styles.actionBtnText}>🔄 Restart</Text>
                    </TouchableOpacity>
                  </View>
                </View>
              );
            })}
          </View>
        )}

        {/* 3. CHAOS DRILLS TAB */}
        {activeTab === 'chaos' && (
          <View>
            <Text style={styles.sectionHeading}>Trigger Fault Injection Drills</Text>
            <View style={styles.card}>
              <Text style={styles.cardSubtitle}>Language-Targeted Fault Drills (60s)</Text>
              <View style={styles.chipGrid}>
                <TouchableOpacity style={[styles.chip, { borderColor: '#f97316' }]} onPress={() => triggerFault('JAVA-ERR-SPIKE', 'error_spike', 'java')}>
                  <Text style={[styles.chipText, { color: '#f97316' }]}>🟠 Java 5xx Spike</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#3b82f6' }]} onPress={() => triggerFault('PY-LATENCY', 'high_latency', 'python')}>
                  <Text style={[styles.chipText, { color: '#3b82f6' }]}>🔵 Python High Latency</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#d946ef' }]} onPress={() => triggerFault('RUST-CHAOS', 'intermittent_errors', 'rust')}>
                  <Text style={[styles.chipText, { color: '#d946ef' }]}>🟣 Rust Chaos</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#22c55e' }]} onPress={() => triggerFault('NODE-ERR-SPIKE', 'error_spike', 'node')}>
                  <Text style={[styles.chipText, { color: '#22c55e' }]}>🟢 Node 5xx Spike</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#06b6d4' }]} onPress={() => triggerFault('GO-ERR-SPIKE', 'error_spike', 'go')}>
                  <Text style={[styles.chipText, { color: '#06b6d4' }]}>🩵 Go 5xx Spike</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#8b5cf6' }]} onPress={() => triggerFault('DOTNET-ERR-SPIKE', 'error_spike', 'dotnet')}>
                  <Text style={[styles.chipText, { color: '#8b5cf6' }]}>💜 .NET 5xx Spike</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#64748b' }]} onPress={() => triggerFault('C-ERR-SPIKE', 'error_spike', 'c')}>
                  <Text style={[styles.chipText, { color: '#cbd5e1' }]}>⚙️ C 5xx Spike</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[styles.chip, { borderColor: '#ef4444' }]} onPress={() => triggerFault('FULL-OUTAGE', 'service_outage', 'all', 30)}>
                  <Text style={[styles.chipText, { color: '#ef4444' }]}>🔴 Full Stack Outage (30s)</Text>
                </TouchableOpacity>
              </View>
            </View>

            <View style={styles.card}>
              <Text style={styles.cardSubtitle}>Target Crashes (Process / Thread Kill)</Text>
              <View style={styles.chipGrid}>
                <TouchableOpacity style={styles.crashBtn} onPress={() => triggerCrash('java', 'process')}>
                  <Text style={styles.crashBtnText}>💥 Java Process Crash</Text>
                </TouchableOpacity>
                <TouchableOpacity style={styles.crashBtn} onPress={() => triggerCrash('python', 'process')}>
                  <Text style={styles.crashBtnText}>💥 Python Process Crash</Text>
                </TouchableOpacity>
                <TouchableOpacity style={styles.crashBtn} onPress={() => triggerCrash('c', 'process')}>
                  <Text style={styles.crashBtnText}>💥 C Process Crash</Text>
                </TouchableOpacity>
                <TouchableOpacity style={styles.crashBtn} onPress={() => triggerCrash('java', 'thread')}>
                  <Text style={styles.crashBtnText}>🧵 Java Thread Fault</Text>
                </TouchableOpacity>
              </View>
            </View>
          </View>
        )}

        {/* 4. DEPENDENCIES TAB */}
        {activeTab === 'dependencies' && (
          <View>
            <View style={styles.rowBetween}>
              <Text style={styles.sectionHeading}>Stack Dependencies Health</Text>
              <TouchableOpacity style={styles.actionBtnSm} onPress={fetchDeps}>
                <Text style={styles.actionBtnTextSm}>🔄 Re-probe</Text>
              </TouchableOpacity>
            </View>

            {refreshing && <ActivityIndicator size="small" color="#38bdf8" style={{ marginVertical: 10 }} />}

            {dependencies?.dependencies?.map((dep) => (
              <View key={dep.id} style={[styles.card, { borderLeftWidth: 3, borderLeftColor: dep.available ? '#10b981' : '#ef4444' }]}>
                <View style={styles.rowBetween}>
                  <View>
                    <Text style={styles.depName}>{dep.name}</Text>
                    <Text style={styles.depRole}>{dep.role}</Text>
                  </View>
                  <Text style={[styles.pill, { backgroundColor: dep.available ? '#064e3b' : '#7f1d1d', color: dep.available ? '#34d399' : '#f87171' }]}>
                    {dep.status}
                  </Text>
                </View>
                <View style={styles.rowBetween}>
                  <Text style={styles.depMetaLabel}>Endpoint:</Text>
                  <Text style={styles.valMono}>{dep.endpoint}</Text>
                </View>
                <View style={styles.rowBetween}>
                  <Text style={styles.depMetaLabel}>Latency:</Text>
                  <Text style={[styles.valMono, { color: dep.latency_ms < 20 ? '#10b981' : '#f59e0b' }]}>{dep.latency_ms} ms</Text>
                </View>
                <Text style={styles.depCapacity}>{dep.capacity_usage}</Text>
              </View>
            ))}
          </View>
        )}

        {/* 5. SQL QUERY TAB */}
        {activeTab === 'sql' && (
          <View>
            <Text style={styles.sectionHeading}>PostgreSQL SQL Query Runner</Text>

            <View style={styles.card}>
              <Text style={styles.cardSubtitle}>Saved Curated Queries</Text>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginBottom: 10 }}>
                {savedQueries.map((q) => (
                  <TouchableOpacity key={q.id} style={styles.savedQueryChip} onPress={() => setSqlQuery(q.sql)}>
                    <Text style={styles.savedQueryChipText}>{q.name}</Text>
                  </TouchableOpacity>
                ))}
              </ScrollView>

              <Text style={styles.label}>SQL Query:</Text>
              <TextInput
                style={styles.sqlInput}
                multiline
                numberOfLines={4}
                value={sqlQuery}
                onChangeText={setSqlQuery}
                autoCapitalize="none"
                autoCorrect={false}
              />

              <TouchableOpacity style={styles.btnRunSql} onPress={executeSql} disabled={sqlLoading}>
                {sqlLoading ? <ActivityIndicator color="#041226" /> : <Text style={styles.btnRunSqlText}>▶ Run Query</Text>}
              </TouchableOpacity>
            </View>

            {sqlResult && (
              <View style={styles.card}>
                <View style={styles.rowBetween}>
                  <Text style={styles.cardTitle}>Query Results</Text>
                  <Text style={styles.valMono}>{sqlResult.execution_time_ms || 0} ms ({sqlResult.row_count || 0} rows)</Text>
                </View>

                {sqlResult.error ? (
                  <Text style={{ color: '#ef4444', fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace', marginTop: 8 }}>
                    {sqlResult.error}
                  </Text>
                ) : (
                  <ScrollView horizontal style={{ marginTop: 8 }}>
                    <View>
                      {/* Grid Header */}
                      <View style={styles.gridHeaderRow}>
                        <Text style={[styles.gridCellHeader, { width: 40 }]}>#</Text>
                        {sqlResult.columns?.map((col, idx) => (
                          <Text key={idx} style={styles.gridCellHeader}>{col}</Text>
                        ))}
                      </View>
                      {/* Grid Body */}
                      {sqlResult.rows?.map((row, rIdx) => (
                        <View key={rIdx} style={styles.gridRow}>
                          <Text style={[styles.gridCell, { width: 40, color: '#64748b' }]}>{rIdx + 1}</Text>
                          {row.map((cell, cIdx) => (
                            <Text key={cIdx} style={styles.gridCell} numberOfLines={1}>
                              {cell === null ? '<NULL>' : String(cell)}
                            </Text>
                          ))}
                        </View>
                      ))}
                    </View>
                  </ScrollView>
                )}
              </View>
            )}
          </View>
        )}

        <View style={{ height: 40 }} />
      </ScrollView>

      {/* Settings / Connection Modal */}
      <Modal visible={settingsVisible} animationType="slide" transparent>
        <View style={styles.modalOverlay}>
          <View style={styles.modalBox}>
            <Text style={styles.modalTitle}>⚙️ Canary Server Connection</Text>
            <Text style={styles.modalDesc}>
              Enter the IP address of your Canary server (e.g. your local Wi-Fi IP or emulator host).
            </Text>

            <TextInput
              style={styles.modalInput}
              value={inputUrl}
              onChangeText={setInputUrl}
              autoCapitalize="none"
              autoCorrect={false}
              placeholder="http://192.168.1.100:8085"
              placeholderTextColor="#64748b"
            />

            <View style={{ flexDirection: 'row', gap: 8, marginVertical: 8 }}>
              <TouchableOpacity style={styles.presetBtn} onPress={() => setInputUrl('http://localhost:8085')}>
                <Text style={styles.presetBtnText}>localhost</Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.presetBtn} onPress={() => setInputUrl('http://10.0.2.2:8085')}>
                <Text style={styles.presetBtnText}>10.0.2.2 (Android)</Text>
              </TouchableOpacity>
            </View>

            <View style={{ flexDirection: 'row', justifyContent: 'flex-end', gap: 10, marginTop: 12 }}>
              <TouchableOpacity style={styles.modalCancelBtn} onPress={() => setSettingsVisible(false)}>
                <Text style={styles.modalBtnText}>Cancel</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={styles.modalSaveBtn}
                onPress={() => {
                  setServerUrl(inputUrl);
                  setSettingsVisible(false);
                }}
              >
                <Text style={[styles.modalBtnText, { color: '#041226', fontWeight: '700' }]}>Save & Connect</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#090e17',
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: '#24344d',
  },
  titleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  statusDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
  },
  headerTitle: {
    color: '#f1f5f9',
    fontSize: 17,
    fontWeight: '700',
  },
  settingsBtn: {
    backgroundColor: '#151f30',
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: '#24344d',
  },
  settingsBtnText: {
    color: '#94a3b8',
    fontSize: 12,
    fontWeight: '600',
  },
  serverBanner: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    backgroundColor: '#0f172a',
    paddingHorizontal: 16,
    paddingVertical: 6,
    borderBottomWidth: 1,
    borderBottomColor: '#1e293b',
  },
  serverUrlText: {
    color: '#94a3b8',
    fontSize: 11,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
    flex: 1,
  },
  connPill: {
    fontSize: 10,
    fontWeight: '700',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    overflow: 'hidden',
  },
  navBar: {
    flexDirection: 'row',
    backgroundColor: '#111a29',
    borderBottomWidth: 1,
    borderBottomColor: '#24344d',
  },
  navTab: {
    flex: 1,
    paddingVertical: 10,
    alignItems: 'center',
  },
  activeNavTab: {
    borderBottomWidth: 2,
    borderBottomColor: '#38bdf8',
  },
  navTabText: {
    color: '#94a3b8',
    fontSize: 12,
    fontWeight: '600',
  },
  activeNavTabText: {
    color: '#38bdf8',
    fontWeight: '700',
  },
  content: {
    flex: 1,
    padding: 12,
  },
  faultAlertBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: '#7f1d1d',
    borderWidth: 1,
    borderColor: '#ef4444',
    padding: 10,
    borderRadius: 6,
    marginBottom: 12,
  },
  faultAlertTitle: {
    color: '#fee2e2',
    fontWeight: '800',
    fontSize: 12,
  },
  faultAlertSub: {
    color: '#fecaca',
    fontSize: 11,
  },
  abortBtn: {
    backgroundColor: '#f59e0b',
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 4,
  },
  abortBtnText: {
    color: '#000',
    fontWeight: '700',
    fontSize: 11,
  },
  kpiGrid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
    marginBottom: 12,
  },
  kpiCard: {
    width: '48%',
    backgroundColor: '#151f30',
    borderWidth: 1,
    borderColor: '#24344d',
    borderTopWidth: 3,
    padding: 10,
    borderRadius: 6,
  },
  kpiLabel: {
    color: '#94a3b8',
    fontSize: 10,
    fontWeight: '700',
  },
  kpiValue: {
    color: '#f1f5f9',
    fontSize: 20,
    fontWeight: '800',
    marginVertical: 2,
  },
  kpiSub: {
    color: '#64748b',
    fontSize: 10,
  },
  card: {
    backgroundColor: '#151f30',
    borderWidth: 1,
    borderColor: '#24344d',
    borderRadius: 6,
    padding: 12,
    marginBottom: 12,
  },
  cardTitle: {
    color: '#f1f5f9',
    fontSize: 14,
    fontWeight: '700',
    marginBottom: 8,
  },
  cardSubtitle: {
    color: '#94a3b8',
    fontSize: 12,
    fontWeight: '700',
    marginBottom: 8,
  },
  sectionHeading: {
    color: '#f1f5f9',
    fontSize: 15,
    fontWeight: '700',
    marginBottom: 10,
  },
  rowBetween: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginVertical: 3,
  },
  rowAlignCenter: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
  label: {
    color: '#94a3b8',
    fontSize: 12,
  },
  valMono: {
    color: '#7dd3fc',
    fontSize: 12,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
    fontWeight: '600',
  },
  recentRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 5,
    borderBottomWidth: 1,
    borderBottomColor: '#1e293b',
    gap: 6,
  },
  badge: {
    fontSize: 9,
    fontWeight: '800',
    paddingHorizontal: 5,
    paddingVertical: 1,
    borderRadius: 3,
    color: '#fff',
  },
  badge_python: { backgroundColor: '#1e3a8a' },
  badge_java: { backgroundColor: '#7c2d12' },
  badge_rust: { backgroundColor: '#701a75' },
  badge_node: { backgroundColor: '#064e3b' },
  badge_go: { backgroundColor: '#083344' },
  badge_dotnet: { backgroundColor: '#2e1065' },
  badge_c: { backgroundColor: '#1e293b' },
  methodText: {
    color: '#38bdf8',
    fontSize: 11,
    fontWeight: '700',
  },
  pathText: {
    color: '#cbd5e1',
    fontSize: 11,
    flex: 1,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
  },
  statusText: {
    fontSize: 11,
    fontWeight: '700',
  },
  latText: {
    color: '#94a3b8',
    fontSize: 10,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
  },
  serviceCard: {
    backgroundColor: '#151f30',
    borderWidth: 1,
    borderColor: '#24344d',
    borderRadius: 6,
    padding: 12,
    marginBottom: 10,
  },
  serviceTitle: {
    color: '#f1f5f9',
    fontSize: 14,
    fontWeight: '700',
  },
  pill: {
    fontSize: 10,
    fontWeight: '700',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
  },
  serviceStatsRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: '#0f172a',
    borderRadius: 4,
    padding: 8,
    marginVertical: 8,
  },
  statBox: {
    alignItems: 'center',
  },
  statBoxLabel: {
    color: '#64748b',
    fontSize: 9,
    textTransform: 'uppercase',
    fontWeight: '700',
  },
  statBoxVal: {
    color: '#f1f5f9',
    fontSize: 12,
    fontWeight: '700',
    marginTop: 2,
  },
  serviceActionRow: {
    flexDirection: 'row',
    gap: 8,
    marginTop: 4,
  },
  actionBtn: {
    flex: 1,
    paddingVertical: 6,
    alignItems: 'center',
    borderRadius: 4,
  },
  btnStop: {
    backgroundColor: '#dc2626',
  },
  btnStart: {
    backgroundColor: '#10b981',
  },
  btnRestart: {
    backgroundColor: '#24344d',
    flex: 0.5,
  },
  actionBtnText: {
    color: '#fff',
    fontSize: 11,
    fontWeight: '700',
  },
  actionBtnSm: {
    backgroundColor: '#151f30',
    borderWidth: 1,
    borderColor: '#24344d',
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 4,
  },
  actionBtnTextSm: {
    color: '#38bdf8',
    fontSize: 11,
    fontWeight: '600',
  },
  chipGrid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 6,
  },
  chip: {
    backgroundColor: '#111a29',
    borderWidth: 1,
    paddingHorizontal: 8,
    paddingVertical: 6,
    borderRadius: 14,
  },
  chipText: {
    fontSize: 11,
    fontWeight: '600',
  },
  crashBtn: {
    backgroundColor: '#7f1d1d',
    borderWidth: 1,
    borderColor: '#ef4444',
    paddingHorizontal: 10,
    paddingVertical: 8,
    borderRadius: 4,
    width: '48%',
    alignItems: 'center',
  },
  crashBtnText: {
    color: '#fee2e2',
    fontSize: 11,
    fontWeight: '700',
  },
  depName: {
    color: '#f1f5f9',
    fontSize: 13,
    fontWeight: '700',
  },
  depRole: {
    color: '#64748b',
    fontSize: 10,
  },
  depMetaLabel: {
    color: '#64748b',
    fontSize: 11,
  },
  depCapacity: {
    color: '#94a3b8',
    fontSize: 10,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
    marginTop: 4,
  },
  savedQueryChip: {
    backgroundColor: '#1e293b',
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 4,
    marginRight: 6,
  },
  savedQueryChipText: {
    color: '#38bdf8',
    fontSize: 11,
    fontWeight: '600',
  },
  sqlInput: {
    backgroundColor: '#090e17',
    borderWidth: 1,
    borderColor: '#24344d',
    borderRadius: 4,
    color: '#7dd3fc',
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
    fontSize: 12,
    padding: 8,
    minHeight: 70,
    textAlignVertical: 'top',
    marginVertical: 6,
  },
  btnRunSql: {
    backgroundColor: '#38bdf8',
    paddingVertical: 8,
    alignItems: 'center',
    borderRadius: 4,
    marginTop: 4,
  },
  btnRunSqlText: {
    color: '#041226',
    fontWeight: '700',
    fontSize: 13,
  },
  gridHeaderRow: {
    flexDirection: 'row',
    backgroundColor: '#1e293b',
    borderBottomWidth: 1,
    borderBottomColor: '#334155',
  },
  gridCellHeader: {
    color: '#94a3b8',
    fontWeight: '700',
    fontSize: 11,
    width: 120,
    padding: 6,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
  },
  gridRow: {
    flexDirection: 'row',
    borderBottomWidth: 1,
    borderBottomColor: '#1e293b',
  },
  gridCell: {
    color: '#cbd5e1',
    fontSize: 11,
    width: 120,
    padding: 6,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
  },
  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.7)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: 20,
  },
  modalBox: {
    backgroundColor: '#151f30',
    borderWidth: 1,
    borderColor: '#24344d',
    borderRadius: 8,
    padding: 16,
    width: '100%',
    maxWidth: 400,
  },
  modalTitle: {
    color: '#f1f5f9',
    fontSize: 16,
    fontWeight: '700',
    marginBottom: 6,
  },
  modalDesc: {
    color: '#94a3b8',
    fontSize: 12,
    marginBottom: 10,
  },
  modalInput: {
    backgroundColor: '#090e17',
    borderWidth: 1,
    borderColor: '#24344d',
    borderRadius: 4,
    color: '#fff',
    padding: 8,
    fontFamily: Platform.OS === 'ios' ? 'Courier' : 'monospace',
    fontSize: 13,
  },
  presetBtn: {
    backgroundColor: '#1e293b',
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 4,
  },
  presetBtnText: {
    color: '#38bdf8',
    fontSize: 11,
  },
  modalCancelBtn: {
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  modalSaveBtn: {
    backgroundColor: '#38bdf8',
    paddingHorizontal: 14,
    paddingVertical: 6,
    borderRadius: 4,
  },
  modalBtnText: {
    color: '#94a3b8',
    fontSize: 13,
  },
});
