#!/usr/bin/env python3
"""Generate a self-contained interactive HTML report from a JSON or CSV snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from report_csv import load_csv_report


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Dependabot Vulnerability Report</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0b1020;
      --panel: #121933;
      --panel-soft: #1a2347;
      --panel-border: rgba(148, 163, 184, 0.18);
      --text: #e5eefc;
      --muted: #94a3b8;
      --accent: #60a5fa;
      --critical: #dc2626;
      --high: #ea580c;
      --medium: #ca8a04;
      --low: #16a34a;
      --unknown: #7c3aed;
      --shadow: 0 20px 40px rgba(0, 0, 0, 0.25);
      --radius: 18px;
      --renovate: #f59e0b;
      --secret: #ec4899;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background:
        radial-gradient(circle at top right, rgba(96, 165, 250, 0.18), transparent 32%),
        linear-gradient(180deg, #0a0f1d 0%, #0f172a 100%);
      color: var(--text);
      min-height: 100vh;
    }

    .container {
      width: min(1400px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }

    .hero {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: flex-start;
      margin-bottom: 24px;
      padding: 28px;
      background: linear-gradient(180deg, rgba(18, 25, 51, 0.95), rgba(18, 25, 51, 0.82));
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }

    .hero h1 {
      margin: 0 0 10px;
      font-size: clamp(1.8rem, 4vw, 2.7rem);
      line-height: 1.1;
    }

    .hero p {
      margin: 0;
      color: var(--muted);
      max-width: 760px;
    }

    .hero-meta {
      display: grid;
      gap: 10px;
      min-width: 240px;
    }

    .hero-pill,
    .filter-chip,
    .legend-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 8px 12px;
      background: rgba(148, 163, 184, 0.1);
      border: 1px solid rgba(148, 163, 184, 0.14);
      color: var(--text);
      font-size: 0.92rem;
      cursor: pointer;
    }

    .hero-pill {
      cursor: default;
      justify-content: center;
    }

    .layout {
      display: grid;
      gap: 20px;
    }

    .stats-grid,
    .charts-grid {
      display: grid;
      gap: 20px;
    }

    .stats-grid {
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }

    .charts-grid {
      grid-template-columns: 1.15fr 0.85fr;
    }

    .panel {
      background: linear-gradient(180deg, rgba(18, 25, 51, 0.96), rgba(18, 25, 51, 0.82));
      border: 1px solid var(--panel-border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 22px;
      min-width: 0;
    }

    .stat-card {
      padding: 18px;
    }

    .stat-label {
      color: var(--muted);
      font-size: 0.84rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .stat-value {
      display: block;
      margin-top: 10px;
      font-size: clamp(1.5rem, 3vw, 2.1rem);
      font-weight: 700;
    }

    .stat-subtitle {
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 18px;
    }

    .panel-title {
      margin: 0;
      font-size: 1.1rem;
    }

    .panel-subtitle {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 0.93rem;
    }

    .filters {
      display: grid;
      gap: 18px;
    }

    .filter-row {
      display: grid;
      gap: 12px;
    }

    .filter-row label {
      color: var(--muted);
      font-size: 0.92rem;
      font-weight: 600;
    }

    .filter-controls {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .filter-chip input {
      display: none;
    }

    .filter-chip.active {
      border-color: rgba(96, 165, 250, 0.5);
      background: rgba(96, 165, 250, 0.2);
      box-shadow: inset 0 0 0 1px rgba(96, 165, 250, 0.24);
    }

    .repository-search {
      width: min(420px, 100%);
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 12px;
      padding: 12px 14px;
      color: var(--text);
      font-size: 0.96rem;
      outline: none;
    }

    .repository-search:focus {
      border-color: rgba(96, 165, 250, 0.65);
      box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.18);
    }

    .repository-list {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
      max-height: 240px;
      overflow: auto;
      padding-right: 4px;
    }

    .repository-item {
      justify-content: flex-start;
      width: 100%;
      text-align: left;
      font-size: 0.9rem;
    }

    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }

    .chart-wrap {
      position: relative;
      min-height: 320px;
    }

    .bar-chart {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .bar-row {
      display: grid;
      grid-template-columns: minmax(170px, 230px) 1fr 54px;
      gap: 12px;
      align-items: center;
    }

    .bar-label {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--text);
      font-size: 0.92rem;
    }

    .bar-track {
      position: relative;
      height: 16px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(148, 163, 184, 0.15);
    }

    .bar-fill {
      position: absolute;
      top: 0;
      left: 0;
      bottom: 0;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(96, 165, 250, 0.75), rgba(96, 165, 250, 1));
    }

    .bar-value {
      color: var(--muted);
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-size: 0.9rem;
    }

    .donut {
      position: relative;
      width: min(100%, 300px);
      aspect-ratio: 1;
      margin: 0 auto;
      border-radius: 50%;
      background: conic-gradient(#334155 0deg, #334155 360deg);
      display: grid;
      place-items: center;
    }

    .donut::after {
      content: "";
      position: absolute;
      inset: 20%;
      background: rgba(11, 16, 32, 0.95);
      border-radius: 50%;
      border: 1px solid rgba(148, 163, 184, 0.1);
    }

    .donut-center {
      position: relative;
      z-index: 1;
      text-align: center;
      padding: 0 16px;
    }

    .donut-value {
      display: block;
      font-size: clamp(1.8rem, 4vw, 2.4rem);
      font-weight: 700;
    }

    .donut-label {
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .table-toolbar {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }

    thead th {
      text-align: left;
      color: var(--muted);
      padding: 12px 10px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.16);
      position: sticky;
      top: 0;
      background: rgba(18, 25, 51, 0.97);
      backdrop-filter: blur(8px);
      cursor: pointer;
      user-select: none;
    }

    tbody td {
      padding: 12px 10px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      vertical-align: middle;
      font-variant-numeric: tabular-nums;
    }

    tbody tr:hover {
      background: rgba(96, 165, 250, 0.08);
    }

    .severity-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.14);
    }

    .severity-critical { color: #fecaca; background: rgba(220, 38, 38, 0.18); }
    .severity-high { color: #fdba74; background: rgba(234, 88, 12, 0.18); }
    .severity-medium { color: #fde68a; background: rgba(202, 138, 4, 0.18); }
    .severity-low { color: #bbf7d0; background: rgba(22, 163, 74, 0.18); }
    .severity-unknown { color: #ddd6fe; background: rgba(124, 58, 237, 0.18); }

    .repository-links {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 10px;
      margin-top: 6px;
      font-size: 0.78rem;
    }

    .repository-links a {
      color: var(--accent);
      text-decoration: none;
    }

    .repository-links a:hover {
      text-decoration: underline;
    }

    .table-wrap {
      max-height: 560px;
      overflow: auto;
      border-radius: 14px;
      border: 1px solid rgba(148, 163, 184, 0.1);
    }

    .empty-state {
      padding: 28px;
      text-align: center;
      color: var(--muted);
    }

    .footer-note {
      margin-top: 14px;
      color: var(--muted);
      font-size: 0.9rem;
    }

    @media (max-width: 1080px) {
      .charts-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 780px) {
      .container {
        width: min(100vw - 20px, 100%);
        padding-top: 20px;
      }

      .hero {
        flex-direction: column;
      }

      .bar-row {
        grid-template-columns: 1fr;
      }

      .bar-value {
        text-align: left;
      }
    }

    .source-tabs {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 24px;
    }

    .source-tab {
      padding: 8px 18px;
      border-radius: 999px;
      border: 1px solid var(--panel-border);
      background: var(--panel);
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s, color 0.15s, border-color 0.15s;
    }

    .source-tab:hover { background: var(--panel-soft); color: var(--text); }
    .source-tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    .source-tab[data-source="secrets"].active { background: var(--secret); border-color: var(--secret); }
    .source-tab[data-source="renovate"].active { background: var(--renovate); border-color: var(--renovate); }
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <div>
        <h1>GitHub Security Report</h1>
        <p>Interactive view of repository security alerts with live filtering by source, severity and repository name.</p>
      </div>
      <div class="hero-meta">
        <div class="hero-pill" id="generatedAt"></div>
        <div class="hero-pill" id="alertState"></div>
        <div class="hero-pill" id="renovateUser"></div>
      </div>
    </section>

    <div class="source-tabs" id="sourceTabs"></div>

    <div class="layout">
      <section class="stats-grid" id="statsGrid"></section>

      <section class="panel filters">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">Filters</h2>
            <p class="panel-subtitle">Combine severity and repository filters to refine the view.</p>
          </div>
        </div>
        <div class="filter-row">
          <label>Severity levels</label>
          <div class="filter-controls" id="severityFilters"></div>
        </div>
        <div class="filter-row">
          <label for="repositorySearch">Repository search</label>
          <input id="repositorySearch" class="repository-search" type="search" placeholder="Filter repositories by name..." />
        </div>
        <div class="filter-row">
          <label>Repositories</label>
          <div class="filter-controls">
            <button class="filter-chip active" id="selectAllRepos" type="button">Select all</button>
            <button class="filter-chip" id="clearAllRepos" type="button">Clear all</button>
          </div>
          <div class="repository-list" id="repositoryFilters"></div>
        </div>
      </section>

      <section class="charts-grid">
        <article class="panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Top repositories by alerts</h2>
              <p class="panel-subtitle">Only repositories matching the current filters are shown.</p>
            </div>
          </div>
          <div class="chart-wrap bar-chart" id="barChart"></div>
        </article>

        <article class="panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">Severity distribution</h2>
              <p class="panel-subtitle">Totals update as filters change.</p>
            </div>
          </div>
          <div class="chart-wrap">
            <div class="donut" id="donutChart">
              <div class="donut-center">
                <span class="donut-value" id="donutTotal">0</span>
                <div class="donut-label">visible alerts</div>
              </div>
            </div>
          </div>
          <div class="legend" id="severityLegend"></div>
        </article>
      </section>

      <section class="panel">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">Repository details</h2>
            <p class="panel-subtitle">Click column headers to sort.</p>
          </div>
        </div>
        <div class="table-toolbar">
          <div class="legend" id="activeFiltersLegend"></div>
          <div class="footer-note" id="rowCountLabel"></div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th data-sort="repository">Repository</th>
                <th data-sort="critical">Critical</th>
                <th data-sort="high">High</th>
                <th data-sort="medium">Medium</th>
                <th data-sort="low">Low</th>
                <th data-sort="unknown">Unknown</th>
                <th data-sort="secrets">Secrets</th>
                <th data-sort="renovate">Renovate PRs</th>
                <th data-sort="total">Total</th>
              </tr>
            </thead>
            <tbody id="tableBody"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <script>
    const reportData = __REPORT_JSON__;

    const severityConfig = [
      { key: 'critical', label: 'Critical', color: getCss('--critical') },
      { key: 'high', label: 'High', color: getCss('--high') },
      { key: 'medium', label: 'Medium', color: getCss('--medium') },
      { key: 'low', label: 'Low', color: getCss('--low') },
      { key: 'unknown', label: 'Unknown', color: getCss('--unknown') },
    ];

    const sourceConfig = [
      { key: 'all',           label: 'All Security' },
      { key: 'dependabot',    label: 'Dependabot' },
      { key: 'code_scanning', label: 'Code Scanning' },
      { key: 'secrets',       label: 'Secrets' },
      { key: 'renovate',      label: 'Renovate PRs' },
    ];

    const state = {
      source: 'all',
      selectedSeverities: new Set(severityConfig.map(item => item.key)),
      selectedRepositories: new Set(reportData.repositories.map(item => item.repository)),
      repositorySearch: '',
      sortBy: 'total',
      sortDirection: 'desc',
    };

    const elements = {
      generatedAt: document.getElementById('generatedAt'),
      alertState: document.getElementById('alertState'),
      renovateUser: document.getElementById('renovateUser'),
      sourceTabs: document.getElementById('sourceTabs'),
      statsGrid: document.getElementById('statsGrid'),
      severityFilters: document.getElementById('severityFilters'),
      repositoryFilters: document.getElementById('repositoryFilters'),
      repositorySearch: document.getElementById('repositorySearch'),
      selectAllRepos: document.getElementById('selectAllRepos'),
      clearAllRepos: document.getElementById('clearAllRepos'),
      barChart: document.getElementById('barChart'),
      donutChart: document.getElementById('donutChart'),
      donutTotal: document.getElementById('donutTotal'),
      severityLegend: document.getElementById('severityLegend'),
      activeFiltersLegend: document.getElementById('activeFiltersLegend'),
      rowCountLabel: document.getElementById('rowCountLabel'),
      tableBody: document.getElementById('tableBody'),
      tableHeaders: Array.from(document.querySelectorAll('th[data-sort]')),
    };

    init();

    function isSeveritySource() {
      return ['all', 'dependabot', 'code_scanning'].includes(state.source);
    }

    function getSourceData(item) {
      const src = state.source;
      if (src === 'dependabot') {
        return { ...item.dependabot, secrets: 0, renovate: 0 };
      } else if (src === 'code_scanning') {
        return { ...item.code_scanning, secrets: 0, renovate: 0 };
      } else if (src === 'secrets') {
        return { critical: 0, high: 0, medium: 0, low: 0, unknown: 0, secrets: item.secret_scanning, renovate: 0 };
      } else if (src === 'renovate') {
        return { critical: 0, high: 0, medium: 0, low: 0, unknown: 0, secrets: 0, renovate: item.renovate_prs };
      }
      // 'all': merge dependabot + code_scanning
      return {
        critical: item.dependabot.critical + item.code_scanning.critical,
        high:     item.dependabot.high     + item.code_scanning.high,
        medium:   item.dependabot.medium   + item.code_scanning.medium,
        low:      item.dependabot.low      + item.code_scanning.low,
        unknown:  item.dependabot.unknown  + item.code_scanning.unknown,
        secrets:  item.secret_scanning,
        renovate: item.renovate_prs,
      };
    }

    function init() {
      elements.generatedAt.textContent = `Generated: ${formatTimestamp(reportData.generated_at)}`;
      elements.alertState.textContent = `Alert state: ${reportData.alert_state}`;
      if (reportData.renovate_user) {
        elements.renovateUser.textContent = `Renovate user: ${reportData.renovate_user}`;
      }
      renderSourceTabs();
      renderSeverityFilters();
      renderRepositoryFilters();
      renderSeverityLegend();
      elements.tableHeaders.forEach(header => {
        header.dataset.label = header.textContent;
      });
      bindEvents();
      updateView();
    }

    function renderSourceTabs() {
      elements.sourceTabs.innerHTML = '';
      sourceConfig.forEach(src => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `source-tab${state.source === src.key ? ' active' : ''}`;
        btn.dataset.source = src.key;
        btn.textContent = src.label;
        btn.addEventListener('click', () => {
          state.source = src.key;
          renderSourceTabs();
          updateSourceVisibility();
          updateView();
        });
        elements.sourceTabs.appendChild(btn);
      });
    }

    function updateSourceVisibility() {
      const sevMode = isSeveritySource();
      const filtersPanel = elements.severityFilters.closest('.filter-row');
      if (filtersPanel) filtersPanel.style.display = sevMode ? '' : 'none';
      const donutPanel = elements.donutChart.closest('article');
      if (donutPanel) donutPanel.style.display = sevMode ? '' : 'none';
    }

    function bindEvents() {
      elements.repositorySearch.addEventListener('input', event => {
        state.repositorySearch = event.target.value.trim().toLowerCase();
        renderRepositoryFilters();
        updateView();
      });

      elements.selectAllRepos.addEventListener('click', () => {
        reportData.repositories.forEach(item => state.selectedRepositories.add(item.repository));
        renderRepositoryFilters();
        updateView();
      });

      elements.clearAllRepos.addEventListener('click', () => {
        state.selectedRepositories.clear();
        renderRepositoryFilters();
        updateView();
      });

      elements.tableHeaders.forEach(header => {
        header.addEventListener('click', () => {
          const sortBy = header.dataset.sort;
          if (state.sortBy === sortBy) {
            state.sortDirection = state.sortDirection === 'asc' ? 'desc' : 'asc';
          } else {
            state.sortBy = sortBy;
            state.sortDirection = sortBy === 'repository' ? 'asc' : 'desc';
          }
          updateView();
        });
      });
    }

    function renderSeverityFilters() {
      elements.severityFilters.innerHTML = '';
      severityConfig.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `filter-chip ${state.selectedSeverities.has(item.key) ? 'active' : ''}`;
        button.innerHTML = `<span class="dot" style="background:${item.color}"></span>${item.label}`;
        button.addEventListener('click', () => {
          if (state.selectedSeverities.has(item.key)) {
            state.selectedSeverities.delete(item.key);
          } else {
            state.selectedSeverities.add(item.key);
          }
          renderSeverityFilters();
          updateView();
        });
        elements.severityFilters.appendChild(button);
      });
    }

    function renderRepositoryFilters() {
      const search = state.repositorySearch;
      const repositories = reportData.repositories
        .map(item => item.repository)
        .filter(name => name.toLowerCase().includes(search))
        .sort((left, right) => left.localeCompare(right));

      elements.repositoryFilters.innerHTML = '';

      if (repositories.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = 'No repositories match the current search.';
        elements.repositoryFilters.appendChild(empty);
        return;
      }

      repositories.forEach(repository => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `filter-chip repository-item ${state.selectedRepositories.has(repository) ? 'active' : ''}`;
        button.textContent = repository;
        button.addEventListener('click', () => {
          if (state.selectedRepositories.has(repository)) {
            state.selectedRepositories.delete(repository);
          } else {
            state.selectedRepositories.add(repository);
          }
          renderRepositoryFilters();
          updateView();
        });
        elements.repositoryFilters.appendChild(button);
      });

      elements.selectAllRepos.classList.toggle('active', state.selectedRepositories.size === reportData.repositories.length);
      elements.clearAllRepos.classList.toggle('active', state.selectedRepositories.size === 0);
    }

    function renderSeverityLegend() {
      elements.severityLegend.innerHTML = '';
      severityConfig.forEach(item => {
        const chip = document.createElement('div');
        chip.className = 'legend-chip';
        chip.innerHTML = `<span class="dot" style="background:${item.color}"></span>${item.label}`;
        elements.severityLegend.appendChild(chip);
      });
    }

    function updateView() {
      updateSourceVisibility();
      const filteredRows = getFilteredRows();
      const totals = getTotals(filteredRows);
      renderStats(totals, filteredRows);
      renderBarChart(filteredRows);
      renderDonutChart(totals);
      renderActiveFilters();
      renderTable(filteredRows);
      updateSortIndicators();
    }

    function getFilteredRows() {
      const sevMode = isSeveritySource();
      const search = state.repositorySearch;
      return reportData.repositories
        .filter(item => state.selectedRepositories.has(item.repository))
        .filter(item => item.repository.toLowerCase().includes(search))
        .map(item => {
          const data = getSourceData(item);
          let visibleTotal;
          if (sevMode) {
            visibleTotal = severityConfig.reduce((sum, severity) => {
              return sum + (state.selectedSeverities.has(severity.key) ? data[severity.key] : 0);
            }, 0);
          } else if (state.source === 'secrets') {
            visibleTotal = data.secrets;
          } else {
            visibleTotal = data.renovate;
          }
          return { ...item, ...data, visibleTotal };
        })
        .filter(item => item.visibleTotal > 0)
        .sort(sortRows);
    }

    function sortRows(left, right) {
      const direction = state.sortDirection === 'asc' ? 1 : -1;
      const field = state.sortBy;

      if (field === 'repository') {
        return left.repository.localeCompare(right.repository) * direction;
      }

      const leftValue  = field === 'total' ? left.visibleTotal  : (left[field]  ?? 0);
      const rightValue = field === 'total' ? right.visibleTotal : (right[field] ?? 0);

      if (rightValue === leftValue) {
        return left.repository.localeCompare(right.repository);
      }
      return (leftValue - rightValue) * direction;
    }

    function getTotals(rows) {
      return rows.reduce((acc, row) => {
        severityConfig.forEach(severity => {
          acc[severity.key] += isSeveritySource() && state.selectedSeverities.has(severity.key) ? (row[severity.key] ?? 0) : 0;
        });
        acc.secrets  += row.secrets  ?? 0;
        acc.renovate += row.renovate ?? 0;
        acc.total    += row.visibleTotal;
        return acc;
      }, { critical: 0, high: 0, medium: 0, low: 0, unknown: 0, secrets: 0, renovate: 0, total: 0 });
    }

    function renderStats(totals, rows) {
      let cards;
      if (state.source === 'secrets') {
        cards = [
          { label: 'Active Secrets', value: totals.secrets, subtitle: `${rows.length} repositories in view` },
        ];
      } else if (state.source === 'renovate') {
        cards = [
          { label: 'Renovate PRs Open', value: totals.renovate, subtitle: `${rows.length} repositories in view` },
        ];
      } else {
        cards = [
          { label: 'Visible alerts', value: totals.total, subtitle: `${rows.length} repositories in view` },
          { label: 'Critical',        value: totals.critical, subtitle: 'Highest priority fixes' },
          { label: 'High',            value: totals.high,     subtitle: 'Important remediation needed' },
          { label: 'Medium',          value: totals.medium,   subtitle: 'Monitor and schedule fixes' },
          { label: 'Low + Unknown',   value: totals.low + totals.unknown, subtitle: 'Lower urgency findings' },
        ];
        if (state.source === 'all') {
          cards.push({ label: 'Active Secrets',    value: totals.secrets,  subtitle: 'Secret scanning alerts' });
          cards.push({ label: 'Renovate PRs Open', value: totals.renovate, subtitle: 'Open renovate pull requests' });
        }
      }

      elements.statsGrid.innerHTML = cards.map(card => `
        <article class="panel stat-card">
          <span class="stat-label">${escapeHtml(card.label)}</span>
          <span class="stat-value">${formatNumber(card.value)}</span>
          <div class="stat-subtitle">${escapeHtml(card.subtitle)}</div>
        </article>
      `).join('');
    }

    function renderBarChart(rows) {
      const topRows = rows
        .filter(row => row.visibleTotal > 0)
        .slice(0, 12);

      if (topRows.length === 0) {
        elements.barChart.innerHTML = '<div class="empty-state">No alerts match the selected filters.</div>';
        return;
      }

      const maxValue = Math.max(...topRows.map(row => row.visibleTotal), 1);
      elements.barChart.innerHTML = topRows.map(row => {
        const percentage = (row.visibleTotal / maxValue) * 100;
        return `
          <div class="bar-row">
            <div class="bar-label" title="${escapeHtml(row.repository)}">${escapeHtml(row.repository)}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${percentage.toFixed(2)}%"></div></div>
            <div class="bar-value">${formatNumber(row.visibleTotal)}</div>
          </div>
        `;
      }).join('');
    }

    function renderDonutChart(totals) {
      if (!isSeveritySource()) return;
      const total = totals.total;
      elements.donutTotal.textContent = formatNumber(total);

      if (total === 0) {
        elements.donutChart.style.background = 'conic-gradient(#334155 0deg, #334155 360deg)';
        return;
      }

      let current = 0;
      const stops = severityConfig
        .filter(item => state.selectedSeverities.has(item.key) && totals[item.key] > 0)
        .map(item => {
          const slice = (totals[item.key] / total) * 360;
          const start = current;
          current += slice;
          return `${item.color} ${start.toFixed(2)}deg ${current.toFixed(2)}deg`;
        });

      if (stops.length === 0) stops.push('#334155 0deg 360deg');
      elements.donutChart.style.background = `conic-gradient(${stops.join(', ')})`;
    }

    function renderActiveFilters() {
      const activeRepos = state.selectedRepositories.size === reportData.repositories.length
        ? 'all repositories'
        : `${state.selectedRepositories.size} selected`;

      let chips = `<div class="legend-chip">Repositories: ${escapeHtml(activeRepos)}</div>`;

      if (isSeveritySource()) {
        const activeSeverities = severityConfig
          .filter(item => state.selectedSeverities.has(item.key))
          .map(item => item.label)
          .join(', ') || 'none';
        chips = `<div class="legend-chip">Severity: ${escapeHtml(activeSeverities)}</div>` + chips;
      }

      elements.activeFiltersLegend.innerHTML = chips;
    }

    function renderTable(rows) {
      elements.rowCountLabel.textContent = `${rows.length} rows`;

      if (rows.length === 0) {
        elements.tableBody.innerHTML = '<tr><td colspan="9" class="empty-state">No repositories match the current filters.</td></tr>';
        return;
      }

      const sevMode = isSeveritySource();

      function githubLinks(repository) {
        const repoPath = repository.split('/').map(encodeURIComponent).join('/');
        const links = [
          ['Dependabot', 'security/dependabot'],
          ['Code scanning', 'security/code-scanning'],
          ['Secrets', 'security/secret-scanning'],
          ['Renovate PRs', 'pulls'],
        ];
        return `
          <a href="https://github.com/${repoPath}" target="_blank" rel="noopener noreferrer">${escapeHtml(repository)}</a>
          <span class="repository-links">
            ${links.map(([label, section]) =>
              `<a href="https://github.com/${repoPath}/${section}" target="_blank" rel="noopener noreferrer">${label}</a>`
            ).join('')}
          </span>
        `;
      }

      elements.tableBody.innerHTML = rows.map(row => {
        const showSev = sevMode;
        return `
        <tr>
          <td>${githubLinks(row.repository)}</td>
          <td><span class="severity-badge severity-critical">${formatNumber(showSev && state.selectedSeverities.has('critical') ? (row.critical ?? 0) : 0)}</span></td>
          <td><span class="severity-badge severity-high">${formatNumber(showSev && state.selectedSeverities.has('high') ? (row.high ?? 0) : 0)}</span></td>
          <td><span class="severity-badge severity-medium">${formatNumber(showSev && state.selectedSeverities.has('medium') ? (row.medium ?? 0) : 0)}</span></td>
          <td><span class="severity-badge severity-low">${formatNumber(showSev && state.selectedSeverities.has('low') ? (row.low ?? 0) : 0)}</span></td>
          <td><span class="severity-badge severity-unknown">${formatNumber(showSev && state.selectedSeverities.has('unknown') ? (row.unknown ?? 0) : 0)}</span></td>
          <td><span class="severity-badge" style="background:rgba(236,72,153,0.18);color:#f9a8d4">${formatNumber(row.secrets ?? 0)}</span></td>
          <td><span class="severity-badge" style="background:rgba(245,158,11,0.18);color:#fcd34d">${formatNumber(row.renovate ?? 0)}</span></td>
          <td><strong>${formatNumber(row.visibleTotal)}</strong></td>
        </tr>
        `;
      }).join('');
    }

    function updateSortIndicators() {
      elements.tableHeaders.forEach(header => {
        const key = header.dataset.sort;
        const isActive = key === state.sortBy;
        const suffix = isActive ? (state.sortDirection === 'asc' ? ' ↑' : ' ↓') : '';
        header.textContent = `${header.dataset.label}${suffix}`;
      });
    }

    function formatTimestamp(timestamp) {
      const date = new Date(timestamp);
      return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString();
    }

    function formatNumber(value) {
      return new Intl.NumberFormat().format(value);
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function getCss(name) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an interactive HTML report from JSON or CSV")
    parser.add_argument(
        "-i",
        "--input",
        default="report.json",
        help="Path to a JSON, summary CSV, or detailed issues CSV (default: report.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="dependabot-report.html",
        help="Path to the output HTML file (default: dependabot-report.html)",
    )
    return parser.parse_args()


OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.is_absolute() and not input_path.exists() and (OUTPUT_DIR / input_path).exists():
        input_path = OUTPUT_DIR / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        OUTPUT_DIR.mkdir(exist_ok=True)
        output_path = OUTPUT_DIR / output_path

    try:
        if output_path.resolve() in {
            input_path.resolve(), Path(f"{input_path}.metadata.json").resolve(),
        }:
            raise ValueError("Output must not overwrite the input report or its metadata")
        report = (
            load_csv_report(input_path) if input_path.suffix.lower() == ".csv"
            else json.loads(input_path.read_text(encoding="utf-8"))
        )
        payload = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c")
        rendered = HTML_TEMPLATE.replace("__REPORT_JSON__", payload)
        output_path.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, csv.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Generated {output_path} from {input_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())