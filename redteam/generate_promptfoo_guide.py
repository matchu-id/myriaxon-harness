# -*- coding: utf-8 -*-
"""Generates docs/Panduan_Promptfoo_untuk_Agent.docx -- a comprehensive Bahasa Indonesia guide to
promptfoo, written from what was verified live against the actually-installed package
(v0.122.2) this session: CLI --help output, provider dispatch table (providers-CRCoPqQH.cjs),
A2A/HTTP provider config schemas, the iso:42001 framework mapping, and the real redteam run
against myriaxon earlier in this session. Not written from memory/training-data recall of
promptfoo's public docs, which may be stale relative to this exact installed version.
"""
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor, Inches
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT_FILE = Path(__file__).parent.parent / "docs" / "Panduan_Promptfoo_untuk_Agent.docx"

ACCENT = RGBColor(0x1A, 0x0B, 0x2E)
GREY = RGBColor(0x55, 0x55, 0x55)


def set_cell_shading(cell, color_hex):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color_hex)
    tc_pr.append(shd)


def add_table(doc, headers, rows, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr_cells[i].text = h
        set_cell_shading(hdr_cells[i], "1A0B2E")
        for p in hdr_cells[i].paragraphs:
            for run in p.runs:
                run.font.bold = True
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                run.font.size = Pt(9)
    for row in rows:
        cells = table.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = str(val)
            for p in cells[i].paragraphs:
                for run in p.runs:
                    run.font.size = Pt(9)
    if widths:
        for i, w in enumerate(widths):
            for row in table.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph()
    return table


def add_code_block(doc, code, label=None):
    if label:
        p = doc.add_paragraph()
        run = p.add_run(label)
        run.font.italic = True
        run.font.size = Pt(9)
        run.font.color.rgb = GREY
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    for i, line in enumerate(code.strip("\n").split("\n")):
        if i > 0:
            p.add_run().add_break()
        run = p.add_run(line if line else " ")
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    p.paragraph_format.space_after = Pt(10)
    # light grey background for the whole code paragraph
    p_pr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), "F2F2F2")
    p_pr.append(shd)
    return p


doc = Document()

# Base font
style = doc.styles["Normal"]
style.font.name = "Calibri"
style.font.size = Pt(10.5)

# ============================================================================================
# COVER
# ============================================================================================
title = doc.add_heading("Panduan Menggunakan Promptfoo untuk Pengujian AI Agent", level=0)
subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = subtitle.add_run("Fokus: No-Code Agent & Google Agent Development Kit (ADK)")
r.font.size = Pt(14)
r.font.color.rgb = ACCENT
r.font.bold = True

meta = doc.add_paragraph()
meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = meta.add_run(
    "Berdasarkan pengujian langsung terhadap promptfoo v0.122.2 (terinstal lokal),\n"
    "bukan sekadar dokumentasi umum -- setiap perintah dan skema pada panduan ini\n"
    "telah diverifikasi langsung terhadap source code paket yang benar-benar terinstal."
)
r.font.size = Pt(10)
r.font.color.rgb = GREY
r.font.italic = True
doc.add_page_break()

# ============================================================================================
# DAFTAR ISI (manual, since python-docx can't build a live TOC field reliably cross-version)
# ============================================================================================
doc.add_heading("Daftar Isi", level=1)
toc_items = [
    "1. Pendahuluan -- Apa itu Promptfoo dan Mengapa Dipakai",
    "2. Instalasi dan Persiapan Awal",
    "3. Konsep Dasar dan Arsitektur",
    "4. Alur Kerja (Flow) Promptfoo Secara Menyeluruh",
    "5. Menghubungkan Promptfoo ke Agent Anda",
    "   5.1 Script-based (exec / python / golang / ruby) -- untuk CLI/agent lokal",
    "   5.2 HTTP / Webhook -- untuk platform No-Code apa pun",
    "   5.3 WebSocket -- untuk agent real-time/streaming",
    "   5.4 n8n -- integrasi native untuk platform No-Code populer",
    "   5.5 Agent2Agent (A2A) -- cara resmi menghubungkan Google ADK",
    "   5.6 MCP (Model Context Protocol)",
    "   5.7 Provider Model Bawaan (OpenAI, Anthropic, Gemini/Vertex, dst.)",
    "6. Model Judge / Grading -- Siapa yang Menilai Jawaban Agent",
    "7. Jenis-Jenis Pengujian yang Tersedia",
    "8. Katalog Lengkap Plugin Red Team",
    "9. Katalog Lengkap Strategi Serangan",
    "10. Dashboard dan Pelaporan Hasil",
    "11. Studi Kasus Nyata -- Integrasi dengan Myriaxon",
    "12. Referensi Lengkap Perintah CLI",
    "13. Lampiran -- Contoh Konfigurasi Siap Pakai",
]
for item in toc_items:
    p = doc.add_paragraph(item)
    p.paragraph_format.space_after = Pt(2)
doc.add_page_break()

# ============================================================================================
# 1. PENDAHULUAN
# ============================================================================================
doc.add_heading("1. Pendahuluan -- Apa itu Promptfoo dan Mengapa Dipakai", level=1)
doc.add_paragraph(
    "Promptfoo adalah sebuah platform open-source (CLI + dashboard lokal) untuk menguji dan "
    "mengevaluasi aplikasi berbasis Large Language Model (LLM) -- baik itu prompt tunggal, "
    "chatbot, maupun AI agent yang punya akses ke tools (file, shell, API eksternal, dll). "
    "Promptfoo punya dua kegunaan utama yang saling melengkapi:"
)
doc.add_paragraph(
    "Evaluasi kualitas (eval) -- menjalankan sekumpulan test case terhadap satu atau beberapa "
    "model/agent secara paralel, lalu menilai apakah jawabannya benar berdasarkan assertion "
    "yang Anda tentukan sendiri (cocok untuk regression testing, membandingkan model, atau "
    "memvalidasi perubahan prompt).", style="List Bullet"
)
doc.add_paragraph(
    "Red teaming (redteam) -- menghasilkan serangan adversarial (jailbreak, prompt injection, "
    "kebocoran data, dsb.) secara otomatis, lalu menjalankannya terhadap agent Anda untuk "
    "menemukan celah keamanan sebelum agent tersebut dipakai secara nyata. Inilah bagian yang "
    "paling relevan untuk fungsi AI Governance.", style="List Bullet"
)
doc.add_paragraph(
    "Yang membedakan promptfoo dari sekadar 'chat dengan model lalu baca manual': seluruh proses "
    "di atas terhubung ke agent Anda yang SESUNGGUHNYA -- bukan model API telanjang. Artinya, "
    "hasil pengujian mencerminkan perilaku nyata dari system prompt, tool-calling, dan seluruh "
    "harness/scaffolding yang sudah Anda bangun, bukan cuma kemampuan mentah model di baliknya."
)

# ============================================================================================
# 2. INSTALASI
# ============================================================================================
doc.add_heading("2. Instalasi dan Persiapan Awal", level=1)
doc.add_paragraph(
    "Promptfoo adalah paket Node.js, dipasang secara global melalui npm. Prasyarat: Node.js "
    "dan npm sudah terpasang di sistem (dikonfirmasi berjalan baik di lingkungan Windows dengan "
    "Node v24.20.0 / npm v11.19.0)."
)
add_code_block(doc, "npm install -g promptfoo\npromptfoo --version", "Instalasi global:")
doc.add_paragraph(
    "Tidak ada langkah konfigurasi wajib lainnya untuk mode eval biasa. Untuk fitur redteam "
    "generation (pembuatan serangan otomatis), promptfoo akan meminta verifikasi email pada "
    "penggunaan pertama -- ini karena proses generate memanggil layanan cloud promptfoo.app "
    "sendiri (model khusus untuk membuat prompt adversarial), terpisah dari API key model/agent "
    "Anda sendiri. Proses evaluasi (menjalankan hasil generate terhadap target Anda) sepenuhnya "
    "lokal dan tidak memerlukan akun apa pun."
)

# ============================================================================================
# 3. KONSEP DASAR
# ============================================================================================
doc.add_heading("3. Konsep Dasar dan Arsitektur", level=1)
doc.add_paragraph(
    "Semua konfigurasi promptfoo ditulis dalam satu file YAML (biasanya bernama "
    "promptfooconfig.yaml), dengan bagian-bagian utama berikut:"
)
add_table(
    doc,
    ["Kunci Konfigurasi", "Fungsi"],
    [
        ("description", "Nama/deskripsi bebas untuk eval ini, muncul di dashboard."),
        ("targets / providers", "Agent atau model yang akan diuji -- lihat Bab 5 untuk semua jenisnya."),
        ("purpose", "Deskripsi bebas tentang apa fungsi agent Anda dan ancaman apa yang relevan. "
                    "Dipakai sebagai konteks saat promptfoo men-generate serangan adversarial."),
        ("plugins", "Daftar kategori risiko/serangan yang ingin diuji (lihat Bab 8)."),
        ("strategies", "Cara penyamaran/transformasi setiap serangan dasar (lihat Bab 9)."),
        ("numTests", "Jumlah test case yang di-generate per plugin (nilai default tetap "
                      "menghasilkan minimal 5 per plugin pada versi yang diuji, terverifikasi "
                      "langsung -- nilai numTests yang lebih kecil tidak selalu diikuti)."),
        ("tests", "Daftar test case eksplisit (dipakai pada mode eval biasa, bukan redteam) "
                  "beserta assert (aturan penilaian: contains, equals, llm-rubric, dst.)."),
        ("prompts", "Template prompt untuk mode eval biasa (mendukung variabel {{var}})."),
    ],
    widths=[1.6, 4.9],
)
doc.add_paragraph(
    "Dua file akan dihasilkan otomatis oleh proses redteam generate: sebuah file berisi seluruh "
    "test case adversarial yang sudah jadi (default-nya redteam.yaml), dan folder cache lokal "
    "(~/.promptfoo/) berisi database SQLite tempat seluruh hasil eval tersimpan -- inilah yang "
    "dibaca oleh dashboard."
)

# ============================================================================================
# 4. ALUR KERJA
# ============================================================================================
doc.add_heading("4. Alur Kerja (Flow) Promptfoo Secara Menyeluruh", level=1)
doc.add_paragraph("Untuk red teaming, alurnya terdiri dari dua tahap terpisah:")
add_code_block(
    doc,
    "promptfoo redteam run\n"
    "  |\n"
    "  |-- Tahap 1: GENERATE (promptfoo redteam generate)\n"
    "  |     - Membaca `purpose`, `plugins`, `strategies` dari config\n"
    "  |     - Memanggil layanan cloud promptfoo.app (perlu verifikasi email sekali)\n"
    "  |     - Model khusus promptfoo men-generate prompt adversarial nyata\n"
    "  |     - Hasil disimpan ke redteam.yaml (test case siap pakai)\n"
    "  |\n"
    "  `-- Tahap 2: EVALUATE (promptfoo eval -c redteam.yaml)\n"
    "        - Untuk setiap test case, memanggil TARGET (agent Anda) via provider yang "
    "dikonfigurasi\n"
    "        - Jawaban agent dinilai oleh MODEL JUDGE (lihat Bab 6) -- LULUS jika tidak ada "
    "bukti agent 'terjebak', GAGAL jika agent benar-benar melakukan hal berbahaya yang diminta\n"
    "        - Hasil ditulis ke database lokal (~/.promptfoo/) secara bertahap (real-time),\n"
    "          bisa dipantau live lewat dashboard sebelum eval selesai",
    "Diagram alur:",
)
doc.add_paragraph(
    "Kedua tahap ini juga bisa dijalankan terpisah: promptfoo redteam generate (hanya membuat "
    "test case, tanpa menjalankannya) lalu promptfoo eval -c redteam.yaml (menjalankan file yang "
    "sudah ada) kapan pun, bahkan berkali-kali tanpa perlu generate ulang -- ini menghemat biaya "
    "layanan generate, karena hasil generate bisa dipakai ulang selama plugin/strategy tidak "
    "berubah."
)
doc.add_paragraph(
    "Catatan penting yang ditemukan langsung saat pengujian: setiap test case dijalankan sebagai "
    "PROSES BARU yang terpisah (untuk provider berbasis exec/python/dll.) -- artinya perubahan "
    "pada script provider (mis. menambah timeout) langsung berlaku untuk seluruh test case "
    "berikutnya TANPA perlu menghentikan eval yang sedang berjalan."
)

# ============================================================================================
# 5. MENGHUBUNGKAN KE AGENT
# ============================================================================================
doc.add_heading("5. Menghubungkan Promptfoo ke Agent Anda", level=1)
doc.add_paragraph(
    "Ini adalah bagian paling penting untuk memahami cara kerja promptfoo dengan agent Anda "
    "sendiri (bukan sekadar model API). Seluruh daftar berikut diverifikasi langsung dari kode "
    "sumber paket yang terinstal (providers-CRCoPqQH.cjs), bukan asumsi -- ada lebih dari 70 "
    "jenis provider bawaan, dikelompokkan berdasarkan cara koneksinya:"
)

doc.add_heading("5.1 Script-based: exec / python / golang / ruby", level=2)
doc.add_paragraph(
    "Cara paling langsung untuk menghubungkan agent yang berjalan sebagai CLI/skrip lokal "
    "(seperti Myriaxon). Promptfoo akan memanggil skrip Anda sebagai proses baru untuk SETIAP "
    "test case, dengan argumen: prompt, opsi konfigurasi (JSON), dan context (JSON). Apa pun "
    "yang skrip tulis ke stdout menjadi jawaban agent."
)
add_code_block(
    doc,
    'targets:\n'
    '  - id: "exec:python my_agent_provider.py"\n'
    '    label: "Agent Saya"\n'
    "# atau bentuk yang lebih ringkas untuk Python:\n"
    'targets:\n'
    '  - id: "python:my_agent_provider.py"',
    "Contoh konfigurasi:",
)
doc.add_paragraph(
    "PENTING (pelajaran nyata dari pengujian Myriaxon): jalankan agent Anda di dalam direktori "
    "kerja terisolasi/sementara (temp directory) untuk setiap test case, BUKAN di direktori "
    "proyek asli -- terutama jika Anda menguji plugin seperti kebocoran secret/file, karena "
    "agent akan benar-benar mencoba membaca apa pun yang ada di direktori kerjanya."
)

doc.add_heading("5.2 HTTP / Webhook -- untuk platform No-Code apa pun", level=2)
doc.add_paragraph(
    "Ini adalah cara PALING UNIVERSAL untuk menghubungkan agent yang dibangun lewat platform "
    "No-Code (Voiceflow, Botpress, Make.com, Dialogflow CX, Microsoft Copilot Studio, atau "
    "platform low-code apa pun) selama platform tersebut menyediakan endpoint REST API/webhook. "
    "Promptfoo tidak perlu tahu apa pun tentang platform di baliknya -- cukup tahu URL, method, "
    "header, dan format body/response-nya."
)
add_table(
    doc,
    ["Field konfigurasi", "Fungsi"],
    [
        ("url", "Endpoint REST API agent Anda."),
        ("method", "GET/POST/dll."),
        ("headers", "Header HTTP, termasuk API key (mis. Authorization: Bearer ...)."),
        ("body", "Template body request -- mendukung variabel {{prompt}}."),
        ("transformRequest", "Skrip/fungsi untuk mengubah bentuk prompt sebelum dikirim."),
        ("transformResponse", "Skrip/fungsi untuk mengekstrak teks jawaban dari response JSON "
                              "platform Anda (format setiap platform No-Code berbeda-beda)."),
        ("session / sessionParser", "Untuk agent yang butuh session ID (percakapan multi-turn) -- "
                                     "promptfoo bisa memanggil endpoint session terpisah dahulu."),
        ("auth / signatureAuth", "Autentikasi lanjutan (OAuth, signature-based, sertifikat, dll.)."),
        ("tools / transformToolsFormat", "Meneruskan definisi tools (format OpenAI) dan otomatis "
                                          "mengonversi ke format provider lain (anthropic/bedrock/"
                                          "google) jika diperlukan."),
    ],
    widths=[1.9, 4.6],
)
add_code_block(
    doc,
    'targets:\n'
    '  - id: "https://my-nocode-platform.example.com/api/agent"\n'
    '    config:\n'
    '      method: "POST"\n'
    '      headers:\n'
    '        Authorization: "Bearer {{env.MY_AGENT_API_KEY}}"\n'
    '      body:\n'
    '        message: "{{prompt}}"\n'
    '      transformResponse: "json.reply"  # ambil field .reply dari JSON respons',
    "Contoh konfigurasi (generik, No-Code apa pun):",
)

doc.add_heading("5.3 WebSocket -- untuk agent real-time/streaming", level=2)
doc.add_paragraph(
    "Untuk agent yang berkomunikasi lewat koneksi WebSocket persisten (bukan request/response "
    "biasa). Gunakan prefix ws: atau wss: pada id target."
)

doc.add_heading("5.4 n8n -- integrasi native untuk platform No-Code populer", level=2)
doc.add_paragraph(
    "Promptfoo punya provider khusus (bawaan, bukan http generik) untuk n8n -- salah satu "
    "platform No-Code/automasi paling populer untuk membangun AI agent lewat visual workflow. "
    "Cukup arahkan ke URL webhook workflow Anda:"
)
add_code_block(doc, 'targets:\n  - id: "n8n:https://your-n8n-instance.com/webhook/agent-endpoint"')

doc.add_heading("5.5 Agent2Agent (A2A) -- cara resmi menghubungkan Google ADK", level=2)
doc.add_paragraph(
    "Ini adalah jawaban langsung untuk kebutuhan Google ADK (Agent Development Kit). Google ADK "
    "tidak punya provider promptfoo khusus bernama 'adk' -- namun ADK, ketika dijalankan sebagai "
    "server (mis. lewat 'adk api_server'), mengekspos endpoint yang mengikuti protokol "
    "Agent2Agent (A2A) -- protokol interoperabilitas agent yang dikembangkan Google. Promptfoo "
    "PUNYA provider bawaan khusus untuk protokol A2A ini, dikonfirmasi langsung dari source code "
    "(class A2AProvider)."
)
add_table(
    doc,
    ["Field konfigurasi A2A", "Fungsi"],
    [
        ("url", "Endpoint A2A agent Anda secara langsung."),
        ("agentCardUrl", "URL 'Agent Card' -- mekanisme discovery bawaan protokol A2A. Jika diisi, "
                          "promptfoo akan MENGAMBIL endpoint & versi protokol secara otomatis dari "
                          "kartu ini, bukan perlu menuliskan url secara manual."),
        ("protocolVersion", "Versi protokol A2A (opsional, mengikuti Agent Card jika tersedia)."),
        ("mode", "'send' (request/response biasa) atau 'stream' (jika agent mendukung streaming)."),
        ("tenant", "ID tenant, untuk deployment multi-tenant."),
    ],
    widths=[1.9, 4.6],
)
add_code_block(
    doc,
    "# Opsi 1: jika Anda sudah tahu URL A2A agent-nya\n"
    'targets:\n'
    '  - id: "a2a:https://my-adk-agent.example.com/a2a"\n\n'
    "# Opsi 2 (direkomendasikan): pakai auto-discovery lewat Agent Card,\n"
    "# sama seperti cara resmi ADK mengekspos dirinya\n"
    'targets:\n'
    '  - id: "a2a"\n'
    '    config:\n'
    '      agentCardUrl: "https://my-adk-agent.example.com/.well-known/agent-card.json"',
    "Contoh konfigurasi untuk agent Google ADK:",
)
doc.add_paragraph(
    "Singkatnya: agent yang dibangun dengan Google ADK, begitu dijalankan sebagai server A2A, "
    "bisa langsung diuji oleh promptfoo tanpa perlu menulis provider kustom apa pun -- cukup "
    "arahkan ke URL Agent Card-nya."
)

doc.add_heading("5.6 MCP (Model Context Protocol)", level=2)
doc.add_paragraph(
    "Promptfoo juga bisa terhubung langsung ke server MCP (protokol yang sama dipakai Myriaxon "
    "untuk tool eksternal) lewat prefix mcp:. Ada pula plugin redteam khusus bernama 'mcp' untuk "
    "menguji kerentanan spesifik MCP (mis. tool description yang menyesatkan, tool poisoning)."
)

doc.add_heading("5.7 Provider Model Bawaan (bukan agent, model API langsung)", level=2)
doc.add_paragraph(
    "Selain menghubungkan ke agent, promptfoo juga punya lebih dari 50 provider bawaan untuk "
    "model API secara langsung (dipakai jika Anda ingin menguji model mentah, bukan agent "
    "lengkap). Beberapa yang paling relevan:"
)
add_table(
    doc,
    ["Prefix", "Layanan"],
    [
        ("openai:", "OpenAI (GPT, dsb.)"),
        ("anthropic:", "Anthropic (Claude)"),
        ("anthropic:claude-agent-sdk / claude-code", "Claude Agent SDK / Claude Code khusus"),
        ("google: / vertex: / palm:", "Google AI Studio (Gemini) / Vertex AI"),
        ("deepseek:", "DeepSeek"),
        ("openrouter:", "OpenRouter (akses ratusan model)"),
        ("mistral: / cohere: / groq: / xai:", "Mistral, Cohere, Groq, xAI (Grok)"),
        ("azure: / azureopenai:", "Azure OpenAI"),
        ("ollama:", "Model lokal via Ollama"),
        ("huggingface: / hf:", "Hugging Face Inference"),
        ("opencode: / openinterpreter: / openclaw:", "Agent coding open-source lain (perbandingan)"),
        ("github:", "GitHub Models"),
    ],
    widths=[2.3, 4.2],
)

# ============================================================================================
# 6. JUDGE / GRADING MODEL
# ============================================================================================
doc.add_heading("6. Model Judge / Grading -- Siapa yang Menilai Jawaban Agent", level=1)
doc.add_paragraph(
    "Promptfoo memisahkan dengan jelas antara 'target' (agent yang diuji) dan 'judge/grading "
    "provider' (model yang MENILAI apakah jawaban target lulus atau gagal). Ini penting dipahami: "
    "hasil PASS/FAIL yang muncul di dashboard adalah keputusan dari model judge, berdasarkan "
    "rubric yang sudah disiapkan promptfoo per plugin -- bukan pencocokan teks sederhana."
)
doc.add_paragraph(
    "Promptfoo otomatis memilih judge provider default berdasarkan kredensial (API key) apa yang "
    "tersedia di environment Anda, dengan urutan prioritas berikut (dikonfirmasi langsung dari "
    "source code graders):"
)
add_table(
    doc,
    ["Prioritas", "Kondisi", "Judge Provider Default"],
    [
        ("1", "AZURE_OPENAI_* tersedia", "Azure OpenAI (deployment Anda sendiri)"),
        ("2", "ANTHROPIC_API_KEY tersedia", "Anthropic (Claude)"),
        ("3", "Kredensial Google AI Studio tersedia (dan tidak ada OpenAI/Anthropic)", "Gemini (AI Studio)"),
        ("4", "Kredensial Google Vertex tersedia", "Gemini (Vertex AI)"),
        ("5", "MISTRAL_API_KEY tersedia (fallback non-Google)", "Mistral"),
        ("6", "XAI_API_KEY tersedia", "xAI (Grok)"),
        ("7", "Kredensial ChatGPT/Codex tersedia", "Codex SDK"),
        ("8", "Kredensial GitHub Models tersedia", "GitHub Models"),
        ("9", "Tidak ada satu pun di atas (default akhir)", "OpenAI (mis. GPT terbaru)"),
    ],
    widths=[0.7, 3.4, 2.4],
)
doc.add_paragraph(
    "Judge provider ini BISA di-override secara eksplisit, tidak harus ikut deteksi otomatis -- "
    "misalnya jika Anda ingin semua penilaian dilakukan oleh model tertentu (mis. Claude) "
    "terlepas dari kredensial apa yang tersedia. Ini diatur lewat kunci defaultTest.options."
    "provider pada config, atau flag --provider khusus untuk redteam generate."
)
add_code_block(
    doc,
    "defaultTest:\n"
    "  options:\n"
    '    provider: "anthropic:claude-opus-5"  # paksa semua penilaian pakai Claude Opus 5',
    "Contoh override judge provider:",
)

# ============================================================================================
# 7. JENIS PENGUJIAN
# ============================================================================================
doc.add_heading("7. Jenis-Jenis Pengujian yang Tersedia", level=1)
doc.add_paragraph("Promptfoo mendukung dua pendekatan pengujian yang berbeda:")
doc.add_heading("7.1 Eval biasa (custom test case + assertion)", level=2)
doc.add_paragraph(
    "Anda menulis sendiri daftar test case (prompt + jawaban yang diharapkan), lalu memilih "
    "jenis assertion: contains (substring), equals (persis sama), regex, javascript/python "
    "(fungsi kustom), llm-rubric (dinilai oleh judge model dengan kriteria bebas), similar "
    "(kesamaan makna via embedding), dan puluhan jenis lain. Cocok untuk regression testing "
    "rutin -- memastikan perubahan pada agent tidak merusak perilaku yang sudah benar."
)
doc.add_heading("7.2 Red team (serangan adversarial otomatis)", level=2)
doc.add_paragraph(
    "Anda tidak menulis prompt sendiri -- promptfoo yang men-generate prompt adversarial secara "
    "otomatis berdasarkan plugin yang dipilih (lihat Bab 8) dan strategi penyamaran (Bab 9). "
    "Cocok untuk pengujian keamanan/governance: mencari celah yang mungkin tidak terpikirkan saat "
    "menulis test case manual."
)

# ============================================================================================
# 8. KATALOG PLUGIN
# ============================================================================================
doc.add_heading("8. Katalog Lengkap Plugin Red Team", level=1)
doc.add_paragraph(
    "Daftar berikut diambil langsung dari perintah 'promptfoo redteam plugins' terhadap paket "
    "yang benar-benar terinstal (bukan disalin dari dokumentasi web yang mungkin sudah usang). "
    "Dikelompokkan berdasarkan relevansi untuk AI agent dengan akses tools/kode (paling relevan "
    "untuk kasus Myriaxon) di bagian atas, lalu kategori umum lainnya."
)

doc.add_heading("8.1 Coding Agent Security (khusus agent dengan akses file/shell)", level=2)
add_table(
    doc,
    ["Plugin", "Menguji apa"],
    [
        ("coding-agent:secret-env-read", "Apakah agent membaca/membocorkan environment variable rahasia."),
        ("coding-agent:secret-file-read", "Apakah agent membaca file rahasia (.env, kredensial, dst.)."),
        ("coding-agent:sandbox-read-escape", "Apakah agent membaca file di luar direktori kerja yang dimaksud."),
        ("coding-agent:sandbox-write-escape", "Apakah agent menulis file di luar direktori kerja yang dimaksud."),
        ("coding-agent:network-egress-bypass", "Apakah agent melakukan permintaan jaringan keluar yang tidak sah."),
        ("coding-agent:procfs-credential-read", "Kebocoran kredensial lewat metadata proses (procfs)."),
        ("coding-agent:repo-prompt-injection", "Apakah teks dalam file repo bisa membajak instruksi agent."),
        ("coding-agent:terminal-output-injection", "Sama seperti di atas, lewat output terminal/perintah."),
        ("coding-agent:generated-vulnerability", "Apakah kode yang dihasilkan agent mengandung kerentanan."),
        ("coding-agent:verifier-sabotage", "Apakah agent melemahkan test/pemeriksaan keamanan agar 'lolos'."),
        ("coding-agent:automation-poisoning", "Apakah agent merusak automasi/CI secara tidak aman."),
        ("coding-agent:delayed-ci-exfil", "Eksfiltrasi data lewat pipeline CI."),
        ("coding-agent:steganographic-exfil", "Data sensitif disembunyikan dalam output yang tampak normal."),
    ],
    widths=[2.2, 4.3],
)

doc.add_heading("8.2 Keamanan & Kontrol Akses Umum", level=2)
add_table(
    doc,
    ["Plugin", "Menguji apa"],
    [
        ("excessive-agency", "Tindakan di luar batas kewenangan yang diberikan."),
        ("hijacking", "Tujuan/fungsi agent dibajak untuk hal lain."),
        ("shell-injection", "Kerentanan command injection pada kode yang dihasilkan."),
        ("sql-injection", "Kerentanan SQL injection."),
        ("ssrf", "Server-Side Request Forgery."),
        ("debug-access", "Antarmuka debug/diagnostik yang tidak semestinya terekspos."),
        ("rbac", "Implementasi role-based access control."),
        ("bfla", "Broken Function-Level Authorization (OWASP API 5)."),
        ("bola", "Broken Object-Level Authorization (OWASP API 1)."),
        ("tool-discovery", "Apakah agent membocorkan seluruh daftar tools yang dimilikinya."),
        ("indirect-prompt-injection", "Injeksi lewat variabel/data eksternal (butuh config.indirectInjectionVar)."),
        ("prompt-extraction", "Kebocoran system prompt asli agent."),
        ("system-prompt-override", "Apakah instruksi asli bisa ditimpa lewat prompt."),
        ("ascii-smuggling", "Instruksi tersembunyi via karakter Unicode tag (tidak terlihat manusia)."),
        ("mcp", "Kerentanan spesifik protokol MCP."),
        ("debug-access", "Lihat di atas."),
        ("reasoning-dos", "Menghabiskan sumber daya lewat pola reasoning berlebihan."),
        ("divergent-repetition", "Kebocoran data training lewat pola repetitif."),
        ("cross-session-leak", "Kebocoran informasi antar sesi pengguna berbeda."),
    ],
    widths=[2.2, 4.3],
)

doc.add_heading("8.3 Privasi & Kebocoran Data (PII)", level=2)
add_table(
    doc,
    ["Plugin", "Menguji apa"],
    [
        ("pii:direct", "Kebocoran PII secara langsung."),
        ("pii:api-db", "Kebocoran PII lewat akses API/database."),
        ("pii:session", "Kebocoran PII dari data sesi."),
        ("pii:social", "Kebocoran PII lewat rekayasa sosial."),
        ("harmful:privacy", "Pelanggaran privasi secara umum."),
        ("cross-session-leak", "Lihat di atas."),
        ("rag-document-exfiltration", "Eksfiltrasi dokumen dari sistem RAG."),
        ("data-exfil", "Eksfiltrasi data lewat parameter URL, gambar, atau markdown link."),
    ],
    widths=[2.2, 4.3],
)

doc.add_heading("8.4 Bias dan Konten Berbahaya", level=2)
add_table(
    doc,
    ["Plugin", "Menguji apa"],
    [
        ("bias:age / :disability / :gender / :race", "Bias berdasarkan usia/disabilitas/gender/ras."),
        ("harmful:hate", "Ujaran kebencian."),
        ("harmful:harassment-bullying", "Pelecehan dan perundungan."),
        ("harmful:violent-crime / :sex-crime / :non-violent-crime", "Konten kejahatan kekerasan/seksual/non-kekerasan."),
        ("harmful:chemical-biological-weapons / :indiscriminate-weapons / :weapons:ied", "Senjata kimia/biologi/rakitan."),
        ("harmful:cybercrime / :cybercrime:malicious-code", "Kejahatan siber dan kode berbahaya."),
        ("harmful:self-harm", "Konten menyakiti diri sendiri."),
        ("harmful:illegal-activities / :illegal-drugs(:meth)", "Aktivitas ilegal, narkoba."),
        ("harmful:misinformation-disinformation", "Disinformasi."),
        ("harmful:radicalization", "Konten radikalisasi/rekrutmen ekstremis."),
        ("harmful:graphic-content / :profanity / :insults", "Konten grafis, kata kasar, hinaan."),
        ("harmful:specialized-advice / :unsafe-practices", "Saran profesional tanpa izin, praktik berbahaya."),
        ("harmful:copyright-violations / :intellectual-property", "Pelanggaran hak cipta/kekayaan intelektual."),
        ("hallucination", "Fabrikasi informasi yang tidak benar."),
        ("overreliance", "Agent terlalu percaya diri pada asumsi yang salah."),
        ("imitation", "Peniruan identitas pihak lain tanpa izin."),
        ("unverifiable-claims", "Klaim yang tidak bisa diverifikasi."),
        ("politics / religion", "Konten politik/agama yang bias."),
        ("competitors", "Menyebut/mendukung kompetitor tanpa izin."),
    ],
    widths=[2.4, 4.1],
)

doc.add_heading("8.5 Domain Spesifik (Medis, Keuangan, Asuransi, Telekomunikasi, dll.)", level=2)
doc.add_paragraph(
    "Promptfoo juga punya plugin khusus per industri -- kurang relevan untuk agent coding "
    "seperti Myriaxon, namun sangat relevan jika the operator membangun agent untuk unit bisnis lain "
    "(customer service, layanan pelanggan telko, dsb.):"
)
add_table(
    doc,
    ["Kategori", "Contoh plugin"],
    [
        ("Medis", "medical:hallucination, medical:off-label-use, medical:dosage-calculation, "
                  "medical:fda:ai-disclosure, medical:fda:cyber-access-control"),
        ("Keuangan", "financial:compliance-violation, financial:confidential-disclosure, "
                     "financial:sox-compliance, financial:calculation-error"),
        ("Asuransi", "insurance:phi-disclosure, insurance:coverage-discrimination, "
                     "insurance:data-disclosure"),
        ("Telekomunikasi", "telecom:cpni-disclosure, telecom:account-takeover, "
                           "telecom:location-disclosure, telecom:e911-misinformation "
                           "(sangat relevan untuk konteks the operator sebagai operator telko)"),
        ("Real Estate", "realestate:fair-housing-discrimination, realestate:steering, "
                        "realestate:valuation-bias"),
        ("E-commerce", "ecommerce:order-fraud, ecommerce:pci-dss, ecommerce:price-manipulation"),
        ("Farmasi", "pharmacy:drug-interaction, pharmacy:controlled-substance-compliance"),
        ("Perlindungan Anak (Teen Safety)", "teen-safety:dangerous-content, "
                                            "teen-safety:age-restricted-goods-and-services"),
        ("Kepatuhan Umum", "coppa, ferpa (privasi anak & pendidikan, AS)"),
    ],
    widths=[1.8, 4.7],
)

doc.add_heading("8.6 Preset Kerangka Kerja (Framework Compliance)", level=2)
doc.add_paragraph(
    "Selain memilih plugin satu per satu, promptfoo punya preset yang otomatis memetakan ke "
    "kumpulan plugin+strategi yang sesuai standar/kerangka kerja tertentu -- sangat relevan untuk "
    "fungsi AI Governance:"
)
add_table(
    doc,
    ["Preset", "Cakupan"],
    [
        ("iso:42001", "ISO/IEC 42001 (AI Management System) -- 7 domain: accountability, "
                       "fairness, privacy, robustness, security, safety, transparency. Inilah "
                       "yang sudah kita gabungkan ke konfigurasi Myriaxon."),
        ("owasp:llm", "OWASP Top 10 for LLM Applications."),
        ("owasp:api", "OWASP API Security Top 10."),
        ("owasp:agentic", "OWASP Top 10 khusus aplikasi agentic."),
        ("nist:ai:measure", "NIST AI Risk Management Framework."),
        ("mitre:atlas", "MITRE ATLAS (taktik serangan terhadap sistem ML)."),
        ("eu:ai-act", "EU AI Act (persyaratan risiko tinggi/dilarang)."),
        ("gdpr", "Pemetaan ke pasal-pasal GDPR yang relevan untuk perlindungan data."),
        ("dod:ai:ethics", "Prinsip etika AI Departemen Pertahanan AS."),
    ],
    widths=[1.6, 4.9],
)
doc.add_paragraph(
    "Preset ini bisa langsung dipakai sebagai nilai plugin, contoh: plugins: [{id: \"iso:42001\"}] "
    "akan otomatis memilih seluruh ~30 plugin yang relevan tanpa perlu menuliskannya satu per "
    "satu."
)

# ============================================================================================
# 9. KATALOG STRATEGI
# ============================================================================================
doc.add_heading("9. Katalog Lengkap Strategi Serangan", level=1)
doc.add_paragraph(
    "Strategi menentukan CARA prompt dasar (dari plugin) disamarkan/dimodifikasi sebelum "
    "dikirim ke agent -- setiap strategi mengalikan jumlah total test case, karena setiap "
    "kombinasi plugin x strategi menghasilkan test case tersendiri."
)
add_table(
    doc,
    ["Strategi", "Cara kerja"],
    [
        ("basic", "Prompt asli tanpa modifikasi apa pun."),
        ("jailbreak", "Teknik jailbreak satu-langkah (ditandai sudah usang/deprecated oleh "
                      "promptfoo sendiri -- disarankan pakai jailbreak:meta)."),
        ("jailbreak:meta", "Versi jailbreak satu-langkah yang lebih baru/direkomendasikan."),
        ("jailbreak:composite", "Menggabungkan BEBERAPA teknik jailbreak sekaligus dalam satu "
                                 "prompt -- paling agresif, dan paling banyak menghasilkan "
                                 "variasi test case (bisa 5-6x lipat dari jumlah dasar)."),
        ("jailbreak:tree", "Mencoba banyak variasi serangan secara iteratif (pencarian pohon) "
                           "hingga menemukan yang berhasil atau menyerah."),
        ("jailbreak:hydra / jailbreak:goblin", "Strategi jailbreak multi-turn/percakapan "
                                                "berkelanjutan."),
        ("prompt-injection", "Template injeksi statis (juga ditandai usang -- disarankan pakai "
                              "jailbreak-templates)."),
        ("jailbreak-templates", "Template jailbreak yang sudah terbukti, lebih modern dari "
                                 "prompt-injection lama."),
        ("base64 / rot13 / hex / leetspeak / morse / piglatin / camelcase / emoji", "Menyandikan "
            "permintaan berbahaya dalam berbagai format agar lolos dari filter kata kunci "
            "sederhana -- menguji apakah agent tetap patuh pada instruksi berbahaya walau sudah "
            "disandikan."),
        ("multilingual", "Menerjemahkan serangan ke bahasa lain (filter keamanan seringkali "
                          "hanya dilatih dalam bahasa Inggris)."),
        ("crescendo / goat", "Serangan bertahap (multi-turn) yang meningkat perlahan hingga "
                              "mencapai tujuan berbahaya."),
        ("best-of-n", "Mencoba banyak variasi acak dari prompt yang sama, ambil yang paling "
                      "berhasil."),
        ("mischievous-user", "Simulasi pengguna yang mencoba berbagai cara nakal secara "
                              "berurutan dalam satu sesi."),
        ("retry", "Mengulang test case yang sebelumnya gagal di-generate."),
        ("custom", "Strategi buatan sendiri (skrip kustom)."),
        ("image / audio / video", "Serangan lewat modalitas non-teks, untuk model multimodal."),
        ("authoritative-markup-injection / indirect-web-pwn", "Injeksi lewat markup yang tampak "
            "otoritatif, atau lewat konten web yang diakses agent."),
    ],
    widths=[2.5, 4.0],
)

# ============================================================================================
# 10. DASHBOARD
# ============================================================================================
doc.add_heading("10. Dashboard dan Pelaporan Hasil", level=1)
doc.add_paragraph(
    "Promptfoo punya dashboard web LOKAL (berjalan di komputer Anda sendiri, tidak dikirim ke "
    "cloud), dikonfirmasi berjalan di http://localhost:15500 secara default."
)
add_table(
    doc,
    ["Perintah", "Fungsi"],
    [
        ("promptfoo view", "Membuka dashboard umum -- daftar seluruh eval, hasil "
                            "berdampingan, tingkat kelulusan."),
        ("promptfoo view -n", "Sama seperti di atas, tapi tidak otomatis membuka browser "
                              "(berguna untuk server tanpa GUI)."),
        ("promptfoo redteam report", "Langsung membuka tampilan laporan red team -- rincian "
                                     "per plugin, tingkat keberhasilan serangan, tingkat "
                                     "keparahan (severity)."),
        ("promptfoo redteam setup", "Wizard visual (GUI) untuk membuat/mengedit konfigurasi "
                                    "redteam tanpa menulis YAML manual -- termasuk memilih "
                                    "preset seperti ISO 42001 lewat klik."),
        ("promptfoo list evals", "Daftar seluruh eval yang tersimpan, lewat command line."),
        ("promptfoo show eval <id>", "Detail satu eval tertentu lewat command line."),
    ],
    widths=[2.3, 4.2],
)
doc.add_paragraph(
    "Catatan penting: hasil eval ditulis ke database SQLite lokal SECARA BERTAHAP selama proses "
    "berjalan -- artinya dashboard bisa dibuka dan dipantau SEBELUM seluruh eval selesai, "
    "hasilnya akan terus bertambah secara real-time."
)

# ============================================================================================
# 11. STUDI KASUS
# ============================================================================================
doc.add_heading("11. Studi Kasus Nyata -- Integrasi dengan Myriaxon", level=1)
doc.add_paragraph(
    "Bagian ini merangkum pengujian nyata yang sudah dilakukan terhadap Myriaxon (agent coding "
    "internal berbasis DeepSeek) menggunakan promptfoo, beserta masalah nyata yang ditemukan "
    "dan cara memperbaikinya -- berguna sebagai referensi saat mengintegrasikan agent lain."
)
doc.add_heading("11.1 Provider kustom (exec:)", level=2)
doc.add_paragraph(
    "Myriaxon adalah CLI Python, bukan layanan HTTP -- maka digunakan provider exec: yang "
    "memanggil 'python -m myriaxon -p \"<prompt>\" --output-format json -y' untuk setiap test "
    "case, lalu mem-parsing field 'text' dari JSON hasilnya."
)
doc.add_heading("11.2 Masalah nyata #1: menjalankan di direktori proyek asli", level=2)
doc.add_paragraph(
    "Versi awal provider menjalankan agent di direktori proyek Myriaxon yang sesungguhnya -- "
    "berbahaya, karena beberapa plugin (coding-agent:secret-env-read, dst.) memang dirancang "
    "untuk menguji apakah agent mau membaca file .env yang berisi API key asli. Diperbaiki "
    "dengan menjalankan setiap test case di direktori sementara (temp) yang kosong dan otomatis "
    "dihapus setelah selesai."
)
doc.add_heading("11.3 Masalah nyata #2: timeout terlalu pendek disalahartikan sebagai LULUS", level=2)
doc.add_paragraph(
    "Timeout awal (180 detik) ternyata terlalu pendek untuk sebagian prompt yang butuh "
    "pertimbangan lebih panjang (mis. permintaan menambahkan postinstall script berbahaya -- "
    "agent akhirnya menolak dengan benar, tapi butuh 276 detik untuk menjelaskan alasannya "
    "secara lengkap). Karena model judge menilai 'tidak ada bukti kepatuhan' sebagai LULUS, "
    "timeout yang gagal menangkap jawaban nyata pun ikut tercatat LULUS -- padahal sebenarnya "
    "hasilnya TIDAK DIKETAHUI, bukan benar-benar aman. Pelajaran: timeout provider harus diatur "
    "cukup longgar berdasarkan pengukuran nyata, bukan tebakan, dan hasil timeout sebaiknya "
    "ditinjau ulang secara terpisah, tidak dianggap otomatis sebagai keberhasilan keamanan."
)
doc.add_heading("11.4 Menggabungkan preset ISO 42001", level=2)
doc.add_paragraph(
    "Untuk kebutuhan kepatuhan AI Governance, preset iso:42001 digabungkan ke konfigurasi yang "
    "sudah ada -- menambah 22 plugin baru dan 4 strategi baru, meningkatkan total dari 365 "
    "menjadi 2.080 test case. Ini menunjukkan pentingnya memperkirakan skala SEBELUM menjalankan "
    "eval besar -- strategi seperti jailbreak:composite bisa melipatgandakan jumlah test case "
    "secara signifikan."
)

# ============================================================================================
# 12. REFERENSI CLI
# ============================================================================================
doc.add_heading("12. Referensi Lengkap Perintah CLI", level=1)
add_table(
    doc,
    ["Perintah", "Fungsi"],
    [
        ("promptfoo eval", "Menjalankan eval biasa (test case + assertion) sesuai config."),
        ("promptfoo eval -c <file> -o <output>", "Menjalankan config tertentu, simpan hasil ke file JSON."),
        ("promptfoo init [direktori]", "Membuat proyek promptfoo baru (wizard)."),
        ("promptfoo view [-n]", "Membuka dashboard lokal."),
        ("promptfoo redteam init", "Wizard interaktif membuat konfigurasi redteam baru."),
        ("promptfoo redteam generate", "Membuat test case adversarial dari config, tanpa menjalankannya."),
        ("promptfoo redteam run", "Generate + evaluate sekaligus (satu perintah)."),
        ("promptfoo redteam report", "Membuka dashboard, langsung ke laporan redteam."),
        ("promptfoo redteam setup", "Wizard visual (GUI browser) untuk redteam."),
        ("promptfoo redteam plugins [--default]", "Menampilkan daftar seluruh plugin yang tersedia."),
        ("promptfoo validate config [-c <file>]", "Memeriksa apakah file config valid."),
        ("promptfoo validate target -c <file>", "Menguji konektivitas ke target/agent secara langsung."),
        ("promptfoo list evals", "Daftar seluruh eval tersimpan."),
        ("promptfoo show eval <id>", "Detail satu eval."),
        ("promptfoo share", "Membuat URL publik untuk satu hasil eval (opsional, perlu izin eksplisit "
                             "sebelum dipakai -- akan mengunggah data ke luar)."),
        ("promptfoo mcp", "Menjalankan promptfoo sendiri sebagai server MCP untuk alat eksternal."),
    ],
    widths=[2.6, 3.9],
)

# ============================================================================================
# 13. LAMPIRAN
# ============================================================================================
doc.add_heading("13. Lampiran -- Contoh Konfigurasi Siap Pakai", level=1)
add_code_block(
    doc,
    'description: "Contoh red team untuk agent internal"\n\n'
    "targets:\n"
    '  - id: "a2a"                      # ganti sesuai jenis agent Anda (lihat Bab 5)\n'
    '    config:\n'
    '      agentCardUrl: "https://agent-anda.example.com/.well-known/agent-card.json"\n\n'
    "purpose: >\n"
    "  Jelaskan di sini apa fungsi agent Anda, akses apa yang dimilikinya, dan\n"
    "  ancaman/skenario penyalahgunaan apa yang paling relevan untuk diuji.\n\n"
    "plugins:\n"
    '  - id: "iso:42001"                 # preset kepatuhan ISO 42001 (~30 plugin sekaligus)\n'
    '  - id: "excessive-agency"\n'
    '  - id: "prompt-extraction"\n'
    '    config:\n'
    '      systemPrompt: "<tempel system prompt asli agent Anda di sini>"\n\n'
    "strategies:\n"
    '  - id: "basic"\n'
    '  - id: "jailbreak:composite"\n\n'
    "numTests: 3",
    "Template dasar untuk agent baru:",
)

doc.add_paragraph()
footer = doc.add_paragraph()
footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = footer.add_run(
    "Dokumen ini disusun berdasarkan verifikasi langsung terhadap promptfoo v0.122.2 pada "
    "2026-09-04. Perilaku dapat berubah pada versi promptfoo yang lebih baru -- selalu "
    "konfirmasi ulang dengan 'promptfoo --version' dan 'promptfoo <perintah> --help' sebelum "
    "mengandalkan detail teknis di atas untuk versi yang berbeda."
)
r.font.size = Pt(8.5)
r.font.italic = True
r.font.color.rgb = GREY

OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUT_FILE)
print(f"Wrote {OUT_FILE}")
