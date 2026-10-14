#!/usr/bin/env python3
"""Add the Indonesian `Id` fields to ai-ml-engineer level 01.

The pass is additive: every localisable string keeps its English field and gains an Indonesian
counterpart placed immediately after it, so the JSON key order still reads source-then-translation.
Nothing else in the file is touched, and that is proved twice -- once by tools/guard_localisation.py
(which strips the `Id` keys and re-parses) and once here at the byte level (which deletes the inserted
lines, undoes the one comma they force, and requires the remainder to equal the original file exactly).

Wording follows certivo-rework/LOCALISATION.md, which was derived from the live data-analyst track:
second person `kamu`, short declarative sentences, identifiers and SQL keywords untouched, and
Indonesian number formatting (4,2 and 10.000 rather than 4.2 and 10,000).
"""

from __future__ import annotations

import json
from pathlib import Path

SRC = Path(
    r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\ai-ml-engineer\levels\01.json"
)
DST = SRC.with_name("01.id.json")

NL = chr(10) + chr(10)
ID_KEYS = (
    "titleId",
    "scenarioMarkdownId",
    "promptId",
    "explanationId",
    "textId",
    "instructionsMarkdownId",
    "rubricMarkdownId",
)

TRANSLATIONS: dict[tuple, str] = {
    ("caseStudy", "title"): "Harlow Outdoors: Sebelum Model Pertama",
    ("caseStudy", "scenarioMarkdown"): (
        "Harlow Outdoors, sebuah pengecer perlengkapan kecil, ingin memprediksi pelanggan mana yang "
        "kemungkinan akan mengembalikan produk bahkan sebelum produk itu dikirim. Sebelum model apa pun "
        "bisa dibangun, seseorang harus merepresentasikan setiap pelanggan sebagai data terstruktur yang "
        "bisa dinalar komputer, memahami probabilitas dasar cukup baik untuk menafsirkan sebuah prediksi "
        "secara masuk akal, dan menulis kode yang cukup rapi sehingga rekan satu tim (atau kamu di masa "
        "depan) bisa membangun di atasnya." + NL +
        "Tidak ada bagian di level ini yang membutuhkan library machine learning. Ini adalah fondasinya: "
        "apa sebenarnya satu titik data, apa arti sebuah probabilitas, dan bagaimana menulis tiga atau "
        "empat baris kode yang mengubah tabel pesanan mentah menjadi sesuatu yang suatu hari bisa "
        "dikonsumsi model."
    ),
    # --- q1: feature vector -------------------------------------------------------------------
    ("questions", 0, "prompt"): (
        "Seorang pelanggan direpresentasikan sebagai `[age, num_past_orders, avg_order_value]` — daftar "
        "angka terurut yang mendeskripsikannya. Apa nama representasi ini dalam machine learning?"
    ),
    ("questions", 0, "explanation"): (
        "Vektor fitur adalah satu titik data yang dinyatakan sebagai daftar atribut numerik yang terurut. "
        "Ini adalah unit dasar representasi: model tidak pernah melihat pelanggan, hanya vektor yang "
        "mewakilinya. Urutannya penting, karena kolom ketiga berarti `avg_order_value` hanya berdasarkan "
        "konvensi — tukar dua entri dan vektor itu mendeskripsikan hal lain."
    ),
    ("questions", 0, "options", 0, "text"): (
        "Vektor fitur — satu titik data sebagai daftar atribut yang terurut"
    ),
    ("questions", 0, "options", 1, "text"): (
        "Matriks konfusi — tabel hasil prediksi dibandingkan hasil sebenarnya"
    ),
    ("questions", 0, "options", 2, "text"): (
        "Fungsi kerugian — angka yang mengukur seberapa salah sebuah prediksi"
    ),
    ("questions", 0, "options", 3, "text"): (
        "Hiperparameter — pengaturan yang dipilih sebelum training dimulai"
    ),
    # --- q2: matrix ---------------------------------------------------------------------------
    ("questions", 1, "prompt"): (
        "Kamu menumpuk vektor fitur untuk 1.000 pelanggan menjadi satu tabel, dengan setiap baris adalah "
        "pelanggan dan setiap kolom adalah fitur. Apa sebutan umum untuk tabel ini?"
    ),
    ("questions", 1, "explanation"): (
        "Matriks — kisi angka persegi panjang yang diindeks berdasarkan baris dan kolom. Baris biasanya "
        "adalah contoh dan kolom biasanya adalah fitur. Satu angka yang berdiri sendiri adalah skalar, "
        "itulah sebabnya `avg_order_value` untuk satu pelanggan adalah skalar sementara kolom yang sama "
        "untuk semua pelanggan adalah vektor."
    ),
    ("questions", 1, "options", 0, "text"): "Gradien — vektor turunan parsial dari sebuah fungsi",
    ("questions", 1, "options", 1, "text"): (
        "Matriks — kisi angka persegi panjang, diindeks berdasarkan baris dan kolom"
    ),
    ("questions", 1, "options", 2, "text"): (
        "Kisi hiperparameter — pengaturan yang dicoba selama pencarian"
    ),
    ("questions", 1, "options", 3, "text"): (
        "Skalar — satu besaran tunggal tanpa arah yang melekat padanya"
    ),
    # --- q3: variance -------------------------------------------------------------------------
    ("questions", 2, "prompt"): (
        "Rata-rata jumlah pesanan sebelumnya di semua pelanggan adalah 4,2, tetapi pelanggan individu "
        "berkisar dari 0 sampai 40. Statistik mana yang paling baik menggambarkan seberapa tersebar "
        "nilai-nilai itu?"
    ),
    ("questions", 2, "explanation"): (
        "Varians, atau akar kuadratnya yaitu simpangan baku, mengukur sebaran di sekitar rata-rata. "
        "Rata-rata saja tidak bisa membedakan basis pelanggan yang semuanya memesan sekitar empat kali "
        "dari basis yang terbelah antara tidak pernah memesan dan memesan empat puluh kali, dan kedua "
        "populasi itu membutuhkan model yang sangat berbeda."
    ),
    ("questions", 2, "options", 0, "text"): (
        "Nilai maksimum saja — angka terbesar yang diamati dalam situasi ini"
    ),
    ("questions", 2, "options", 1, "text"): "Modus saja — satu nilai yang paling sering muncul",
    ("questions", 2, "options", 2, "text"): (
        "Simpangan baku — jarak tipikal sebuah nilai dari rata-rata"
    ),
    ("questions", 2, "options", 3, "text"): (
        "Jumlah pelanggan — berapa banyak baris yang dimuat tabel"
    ),
    # --- q4: independent events ---------------------------------------------------------------
    ("questions", 3, "prompt"): (
        "Jika 30% pesanan dikembalikan, dan kemungkinan pengembalian bersifat independen antara dua "
        "pesanan yang tidak terkait, berapa probabilitas bahwa kedua pesanan itu dikembalikan?"
    ),
    ("questions", 3, "explanation"): (
        "Untuk peristiwa independen, probabilitasnya dikalikan: 0,3 × 0,3 = 0,09, sehingga 9%. "
        "Menjumlahkan alih-alih mengalikan adalah kesalahan yang umum — 60% adalah probabilitas bahwa "
        "setidaknya satu dikembalikan menurut pembacaan yang berbeda (saling eksklusif), dan itu melebihi "
        "apa yang bisa dicapai satu pesanan."
    ),
    ("questions", 3, "options", 0, "text"): "60% — kedua probabilitas dijumlahkan",
    ("questions", 3, "options", 1, "text"): (
        "30% — tidak berubah, karena setiap pesanan independen"
    ),
    ("questions", 3, "options", 2, "text"): "15% — rata-rata dari kedua probabilitas",
    ("questions", 3, "options", 3, "text"): "9% — kedua probabilitas dikalikan",
    # --- q5: correlation is not causation -----------------------------------------------------
    ("questions", 4, "prompt"): (
        "Sebuah model menemukan bahwa pelanggan yang menghubungi dukungan juga lebih mungkin mengembalikan "
        "barang, lalu menyimpulkan bahwa menghubungi dukungan menyebabkan pengembalian. Apa cacatnya?"
    ),
    ("questions", 4, "explanation"): (
        "Korelasi tidak berarti kausalitas. Faktor ketiga — misalnya ketidakpuasan produk — bisa secara "
        "independen menaikkan kontak dukungan maupun pengembalian, menghasilkan asosiasi yang kuat tanpa "
        "hubungan kausal di kedua arah. Menetapkan kausalitas membutuhkan eksperimen terkendali atau "
        "model kausal, bukan asosiasi yang diamati."
    ),
    ("questions", 4, "options", 0, "text"): (
        "Faktor ketiga bisa menaikkan keduanya, sehingga asosiasi itu tidak membuktikan apa pun yang "
        "bersifat kausal"
    ),
    ("questions", 4, "options", 1, "text"): (
        "Kekhawatiran kausalitas berlaku untuk kedokteran, bukan untuk data transaksi ritel"
    ),
    ("questions", 4, "options", 2, "text"): (
        "Korelasi dan kausalitas adalah hubungan yang sama dengan dua nama"
    ),
    ("questions", 4, "options", 3, "text"): (
        "Korelasi dengan kekuatan apa pun menurut definisi adalah hubungan kausal"
    ),
    # --- q6: random variable ------------------------------------------------------------------
    ("questions", 5, "prompt"): (
        "\"Apakah pesanan tertentu akan dikembalikan\" bisa bernilai 0 atau 1 dan tidak diketahui "
        "sebelumnya. Apa nama besaran ini dalam probabilitas?"
    ),
    ("questions", 5, "explanation"): (
        "Variabel acak: besaran yang nilainya bergantung pada hasil dari peristiwa yang belum diamati. "
        "Variabel biner seperti ini mengikuti distribusi Bernoulli, yang persis merupakan parameter yang "
        "dilatih untuk diprediksi oleh sebuah pengklasifikasi."
    ),
    ("questions", 5, "options", 0, "text"): (
        "Hiperparameter — nilai yang dipilih sebelum training dimulai"
    ),
    ("questions", 5, "options", 1, "text"): (
        "Variabel acak — nilainya bergantung pada hasil yang belum diamati"
    ),
    ("questions", 5, "options", 2, "text"): (
        "Vektor fitur — daftar atribut terukur yang terurut"
    ),
    ("questions", 5, "options", 3, "text"): (
        "Gradien — arah kenaikan paling curam dari sebuah fungsi"
    ),
    # --- q7: DRY ------------------------------------------------------------------------------
    ("questions", 6, "prompt"): (
        "Kamu menulis sepuluh baris kode pembersihan data yang sama di tiga tempat berbeda. Prinsip apa "
        "yang dilanggar, dan mengapa itu penting?"
    ),
    ("questions", 6, "explanation"): (
        "DRY — Don't Repeat Yourself. Risikonya adalah ketidakkonsistenan: ketika aturan pembersihan "
        "berubah, salah satu dari tiga salinan kemungkinan terlewat, dan pipeline kemudian berperilaku "
        "berbeda tergantung jalur yang dilalui sebuah baris. Itu adalah bug yang lolos dari tes justru "
        "karena setiap salinan terlihat benar secara terpisah."
    ),
    ("questions", 6, "options", 0, "text"): (
        "Tidak ada — menduplikasi kode tidak berbahaya selama setiap salinan berfungsi hari ini, yang "
        "tidak berlaku di sini"
    ),
    ("questions", 6, "options", 1, "text"): (
        "Itu baru penting setelah sebuah proyek melewati seratus ribu baris, yang tidak berlaku di sini"
    ),
    ("questions", 6, "options", 2, "text"): (
        "DRY — logika yang diduplikasi bergeser, sehingga satu salinan diperbaiki dan yang lain tidak"
    ),
    ("questions", 6, "options", 3, "text"): (
        "Pengulangan itu diinginkan, karena membuat program terukur lebih cepat dalam situasi ini"
    ),
    # --- q8: functions ------------------------------------------------------------------------
    ("questions", 7, "prompt"): (
        "Alih-alih menempelkan logika pembersihan data tiga kali, kamu menulisnya sekali sebagai fungsi "
        "dan memanggilnya di ketiga tempat. Apa manfaat utamanya?"
    ),
    ("questions", 7, "explanation"): (
        "Hanya ada satu tempat untuk mengubah logika, sehingga ketiga lokasi pemanggilan tidak bisa "
        "berbeda. Manfaat sekundernya — nama yang mendokumentasikan maksud, dan argumen yang membuat "
        "ketergantungan menjadi eksplisit — berasal dari sebab yang sama, bukan merupakan keuntungan "
        "tersendiri."
    ),
    ("questions", 7, "options", 0, "text"): (
        "Fungsi tidak bisa menerima argumen, sehingga lebih sederhana untuk dinalar dalam situasi ini"
    ),
    ("questions", 7, "options", 1, "text"): (
        "Menyalin lebih aman, karena setiap salinan kemudian bisa disetel secara independen"
    ),
    ("questions", 7, "options", 2, "text"): (
        "Fungsi selalu berjalan seketika, terlepas dari pekerjaan yang dilakukannya"
    ),
    ("questions", 7, "options", 3, "text"): (
        "Satu tempat untuk mengubah logika, sehingga ketiga lokasi pemanggilan tidak bisa berbeda"
    ),
    # --- q9: dictionary lookup ----------------------------------------------------------------
    ("questions", 8, "prompt"): (
        "Kamu perlu mencari catatan pelanggan berdasarkan ID pelanggannya berkali-kali di seluruh dataset "
        "yang besar. Struktur mana yang umumnya menjadi pilihan tepat?"
    ),
    ("questions", 8, "explanation"): (
        "Dictionary, dengan kunci berupa ID pelanggan. Pencarian rata-rata berwaktu konstan alih-alih "
        "pemindaian linear yang dibutuhkan sebuah list, sehingga biayanya berhenti tumbuh seiring ukuran "
        "data. List tetap benar — hanya saja lebih lambat dengan cara yang akan kamu rasakan pada skala "
        "besar."
    ),
    ("questions", 8, "options", 0, "text"): (
        "Dictionary dengan kunci ID pelanggan — pencarian rata-rata berwaktu konstan"
    ),
    ("questions", 8, "options", 1, "text"): (
        "Tuple catatan yang terurut — tidak dapat diubah, dan dicari berdasarkan posisi indeks, yang "
        "tidak berlaku di sini"
    ),
    ("questions", 8, "options", 2, "text"): (
        "List biasa yang dipindai dari depan — pencarian berwaktu linear setiap kali dalam situasi ini"
    ),
    ("questions", 8, "options", 3, "text"): (
        "Satu string yang digabungkan — satu nilai yang menampung setiap catatan"
    ),
    # --- q10: missing values ------------------------------------------------------------------
    ("questions", 9, "prompt"): (
        "Sebuah fungsi pembersihan diam-diam mengubah `avg_order_value` yang hilang menjadi `0`. Mengapa "
        "ini berbahaya bagi sebuah model?"
    ),
    ("questions", 9, "explanation"): (
        "Nol adalah nilai yang nyata dan bermakna — pelanggan yang benar-benar tidak pernah memesan. "
        "Mengubah \"tidak diketahui\" menjadi \"nol\" menciptakan data, dan model tidak bisa membedakan "
        "ciptaan itu dari kebenaran. Nilai yang hilang lebih baik direpresentasikan secara eksplisit, "
        "atau diimputasi dengan sengaja, daripada diam-diam diciutkan menjadi angka yang masuk akal."
    ),
    ("questions", 9, "options", 0, "text"): (
        "Itu aman, karena nol tidak menyumbang apa pun pada rata-rata numerik dalam situasi ini"
    ),
    ("questions", 9, "options", 1, "text"): (
        "Nol adalah nilai nyata, sehingga data yang hilang menjadi tidak bisa dibedakan darinya"
    ),
    ("questions", 9, "options", 2, "text"): (
        "Itu hanya penting untuk kolom teks, bukan untuk kolom numerik seperti ini, yang tidak berlaku di "
        "sini"
    ),
    ("questions", 9, "options", 3, "text"): (
        "Itu tidak berbahaya selama kolomnya nanti diskalakan atau dinormalisasi dalam situasi ini"
    ),
    # --- q11: probability output --------------------------------------------------------------
    ("questions", 10, "prompt"): (
        "Sebuah model mengeluarkan 0,8 untuk pesanan tertentu. Apa arti angka itu?"
    ),
    ("questions", 10, "explanation"): (
        "Itu adalah estimasi probabilitas — keyakinan model bahwa pesanan ini masuk kelas positif — bukan "
        "kepastian dan bukan persentase keyakinan pada model itu sendiri. Apakah 0,8 harus memicu "
        "tindakan bergantung pada biaya positif palsu dibandingkan negatif palsu, yang merupakan "
        "keputusan bisnis dan bukan sifat dari angka tersebut."
    ),
    ("questions", 10, "options", 0, "text"): (
        "Persentase keyakinan pada akurasi keseluruhan model itu sendiri, yang tidak berlaku di sini"
    ),
    ("questions", 10, "options", 1, "text"): (
        "Peringkat pesanan ini dibandingkan semua pesanan lain di dataset, yang tidak berlaku di sini"
    ),
    ("questions", 10, "options", 2, "text"): (
        "Probabilitas perkiraan bahwa pesanan ini termasuk kelas positif"
    ),
    ("questions", 10, "options", 3, "text"): (
        "Sebuah kepastian — model telah menetapkan hasilnya sebagai fakta, yang tidak berlaku di sini"
    ),
    # --- q12: probability versus label --------------------------------------------------------
    ("questions", 11, "prompt"): (
        "Mengapa berguna bagi sebuah model untuk mengeluarkan probabilitas alih-alih hanya label 0 atau 1?"
    ),
    ("questions", 11, "explanation"): (
        "Probabilitas membawa kekuatan bukti, sehingga ambang batas bisa dipilih untuk menyesuaikan biaya "
        "setiap jenis kesalahan, dan prediksi bisa diperingkat untuk menindaklanjuti kasus yang paling "
        "mungkin terlebih dahulu. Label saja membuang informasi itu pada saat dihasilkan, dan informasi "
        "itu tidak bisa dipulihkan di hilir."
    ),
    ("questions", 11, "options", 0, "text"): (
        "Itu menghapus kebutuhan untuk mengevaluasi model pada data yang disisihkan"
    ),
    ("questions", 11, "options", 1, "text"): (
        "Probabilitas itu eksak, sedangkan label keras hanyalah perkiraan dalam situasi ini"
    ),
    ("questions", 11, "options", 2, "text"): (
        "Itu membuat model berlatih lebih cepat dengan menghapus langkah keputusan"
    ),
    ("questions", 11, "options", 3, "text"): (
        "Ambang batas kemudian bisa disetel sesuai biaya setiap jenis kesalahan"
    ),
    # --- q13: zero-based indexing -------------------------------------------------------------
    ("questions", 12, "prompt"): (
        "Dalam Python, apa yang dikembalikan `orders[0]` untuk list bernama `orders`?"
    ),
    ("questions", 12, "explanation"): (
        "Elemen pertama. Python mengindeks dari nol, sehingga indeks 0 adalah item pertama, indeks 1 item "
        "kedua, dan indeks -1 item terakhir. Konvensi selisih satu itu menyebabkan sangat banyak bug "
        "pengindeksan, sehingga lebih baik memastikannya daripada berasumsi."
    ),
    ("questions", 12, "options", 0, "text"): (
        "Elemen pertama list, karena Python mengindeks dari nol"
    ),
    ("questions", 12, "options", 1, "text"): (
        "Elemen terakhir list, karena nol kembali ke akhir pada setiap kondisi yang terukur sejauh ini"
    ),
    ("questions", 12, "options", 2, "text"): (
        "List kosong yang baru, karena belum ada indeks yang dikonsumsi"
    ),
    ("questions", 12, "options", 3, "text"): (
        "Sebuah error, karena indeks list yang valid di Python dimulai dari satu dalam situasi ini"
    ),
    # --- q14: redundant features --------------------------------------------------------------
    ("questions", 13, "prompt"): (
        "Dua fitur di dataset adalah `order_value_usd` dan `order_value_cents`, dan yang kedua persis sama "
        "dengan yang pertama dikalikan 100. Masalah apa yang ditimbulkan ini?"
    ),
    ("questions", 13, "explanation"): (
        "Keduanya berkorelasi sempurna — yang satu tidak membawa informasi yang tidak dimiliki yang lain. "
        "Redundansi itu bisa membuat tidak stabil model yang mengestimasi bobot per fitur, karena "
        "bobotnya bisa digeser antara keduanya tanpa mengubah prediksi apa pun, dan itu membuat koefisien "
        "hasil fitting lebih sulit ditafsirkan."
    ),
    ("questions", 13, "options", 0, "text"): (
        "Itu membuat nilai kedua kolom menjadi tidak valid secara matematis dalam situasi ini"
    ),
    ("questions", 13, "options", 1, "text"): (
        "Keduanya berkorelasi sempurna, sehingga yang satu tidak menambah informasi"
    ),
    ("questions", 13, "options", 2, "text"): (
        "Itu tidak berpengaruh, karena model mengabaikan kolom yang redundan"
    ),
    ("questions", 13, "options", 3, "text"): (
        "Itu menggandakan jumlah baris yang harus digunakan untuk melatih model"
    ),
    # --- q15: version control and tests -------------------------------------------------------
    ("questions", 14, "prompt"): (
        "Kamu mengubah aturan pembersihan data dan setiap hasil model di hilir bergeser. Apa yang akan "
        "membuat perubahan itu lebih aman dan lebih mudah dinalar?"
    ),
    ("questions", 14, "explanation"): (
        "Pipeline dengan kontrol versi dan tes: perubahannya bisa ditinjau sebagai diff, dan tes "
        "menunjukkan apa lagi yang bergeser. Tanpa itu, satu-satunya cara untuk mengetahui radius "
        "dampaknya adalah menjalankan ulang semuanya dan membandingkan hasilnya dengan mata — yang justru "
        "merupakan situasi yang digambarkan pertanyaan ini."
    ),
    ("questions", 14, "options", 0, "text"): (
        "Membekukan aturannya secara permanen, sehingga tidak ada hasil di hilir yang pernah bergeser "
        "pada setiap kondisi yang terukur sejauh ini"
    ),
    ("questions", 14, "options", 1, "text"): (
        "Jeda ujian ulang yang lebih lama, sehingga perubahannya dibuat lebih jarang"
    ),
    ("questions", 14, "options", 2, "text"): (
        "Kontrol versi dan tes, sehingga perubahan dan dampaknya bisa ditinjau"
    ),
    ("questions", 14, "options", 3, "text"): (
        "Dataset yang lebih besar, sehingga setiap perubahan tunggal menjadi kurang berarti secara "
        "proporsional dalam situasi ini"
    ),
    # --- q16: held-out test set ---------------------------------------------------------------
    ("questions", 15, "prompt"): (
        "Mengapa test set yang disisihkan diperlukan, alih-alih mengukur akurasi pada baris yang sama "
        "dengan yang dipelajari model?"
    ),
    ("questions", 15, "explanation"): (
        "Sebuah model bisa menghafal baris training-nya, sehingga error di sana meremehkan error pada hal "
        "apa pun yang baru — kadang secara dramatis. Menyisihkan data dan menilainya mengestimasi "
        "performa pada kasus yang belum pernah dilihat, yang merupakan satu-satunya hal yang akan "
        "dihadapi model yang di-deploy."
    ),
    ("questions", 15, "options", 0, "text"): (
        "Data yang disisihkan berlatih lebih cepat, karena ada lebih sedikit baris untuk diproses, yang "
        "tidak berlaku di sini"
    ),
    ("questions", 15, "options", 1, "text"): (
        "Itu formalitas yang tidak banyak berpengaruh pada angka yang dilaporkan dalam situasi ini"
    ),
    ("questions", 15, "options", 2, "text"): (
        "Itu menghapus kebutuhan untuk membersihkan data sebelum model melihatnya, yang tidak berlaku di "
        "sini"
    ),
    ("questions", 15, "options", 3, "text"): (
        "Error training meremehkan error pada data baru, karena barisnya bisa dihafal"
    ),
    # --- q17: returning None ------------------------------------------------------------------
    ("questions", 16, "prompt"): (
        "Sebuah fungsi mengembalikan `None` alih-alih memunculkan error ketika diberi baris yang cacat. "
        "Apa risiko praktisnya?"
    ),
    ("questions", 16, "explanation"): (
        "Kegagalannya berjalan diam-diam. Sebuah `None` menyebar ke aritmetika atau penggabungan "
        "berikutnya dan muncul jauh dari penyebabnya, atau diam-diam dipaksa menjadi nilai yang terlihat "
        "sah. Memunculkan error di titik masalahnya menjaga diagnosis tetap dekat dengan cacatnya."
    ),
    ("questions", 16, "options", 0, "text"): (
        "Nilai buruk itu berjalan terus dan muncul jauh dari tempat asalnya"
    ),
    ("questions", 16, "options", 1, "text"): (
        "Itu membuat fungsinya lebih lambat, karena None harus diperiksa tipenya"
    ),
    ("questions", 16, "options", 2, "text"): (
        "Itu mencegah fungsi tersebut diuji secara terpisah sebagai unit dalam situasi ini"
    ),
    ("questions", 16, "options", 3, "text"): (
        "None adalah nilai bawaan yang aman yang bisa ditangani setiap fungsi di hilir"
    ),
    # --- q18: class imbalance -----------------------------------------------------------------
    ("questions", 17, "prompt"): (
        "Dataset berisi 10.000 pesanan dan hanya 300 yang dikembalikan. Mengapa ketidakseimbangan itu "
        "penting saat mengukur sebuah pengklasifikasi?"
    ),
    ("questions", 17, "explanation"): (
        "Selalu memprediksi \"tidak dikembalikan\" menghasilkan akurasi 97% tanpa mengidentifikasi apa "
        "pun. Akurasi biasa karena itu adalah ringkasan yang menyesatkan pada data yang tidak seimbang, "
        "dan precision, recall, atau ukuran berbasis kurva diperlukan untuk mengatakan sesuatu yang "
        "berguna tentang perilaku model pada kelas yang penting."
    ),
    ("questions", 17, "options", 0, "text"): (
        "Itu membuat dataset terlalu kecil untuk dibagi menjadi training set dan test set, yang tidak "
        "berlaku di sini"
    ),
    ("questions", 17, "options", 1, "text"): (
        "Selalu memprediksi kelas mayoritas sudah menghasilkan akurasi 97%"
    ),
    ("questions", 17, "options", 2, "text"): (
        "Itu tidak penting, asalkan jumlah total barisnya cukup besar"
    ),
    ("questions", 17, "options", 3, "text"): (
        "Itu hanya penting jika kelas minoritas melebihi lima persen dari baris dalam situasi ini"
    ),
    # --- q19: .mean() -------------------------------------------------------------------------
    ("questions", 18, "prompt"): (
        "Apa yang dihitung metode `.mean()` untuk daftar nilai numerik yang disimpan dalam pandas Series?"
    ),
    ("questions", 18, "explanation"): (
        "Rata-rata aritmetika — jumlah dibagi banyaknya nilai. Itu adalah besaran yang sama yang disebut "
        "rata-rata oleh pertanyaan-pertanyaan sebelumnya, dan itu adalah satu skalar yang merangkum "
        "kecenderungan sentral seluruh kolom."
    ),
    ("questions", 18, "options", 0, "text"): (
        "Nilai tengah dari series yang terurut, dengan mengabaikan seberapa ekstrem ujung-ujungnya dalam "
        "situasi ini"
    ),
    ("questions", 18, "options", 1, "text"): (
        "Nilai yang paling sering muncul, dengan seri diputus berdasarkan kemunculan pertama dalam "
        "situasi ini"
    ),
    ("questions", 18, "options", 2, "text"): (
        "Rata-rata aritmetika — jumlah nilai dibagi banyaknya nilai"
    ),
    ("questions", 18, "options", 3, "text"): (
        "Jumlah nilai yang tidak hilang, dengan nilai numeriknya sendiri diabaikan dalam situasi ini"
    ),
    # --- q20: feature scale -------------------------------------------------------------------
    ("questions", 19, "prompt"): (
        "Mengapa skala sebuah fitur penting ketika dua fitur dibandingkan dalam satu model?"
    ),
    ("questions", 19, "explanation"): (
        "Fitur yang diukur dalam ribuan mendominasi fitur yang diukur dalam angka satuan, sehingga metode "
        "apa pun yang peka terhadap besaran efektif memberi bobot lebih besar pada kolom bersatuan besar "
        "tanpa ada yang memilihnya. Penskalaan ulang menempatkan kedua kolom pada kedudukan yang "
        "sebanding dan membuat pembobotan itu menjadi keputusan pemodelan yang eksplisit alih-alih "
        "kebetulan satuan."
    ),
    ("questions", 19, "options", 0, "text"): (
        "Itu hanya penting ketika sebuah fitur memuat nilai negatif maupun positif, yang tidak berlaku di "
        "sini"
    ),
    ("questions", 19, "options", 1, "text"): (
        "Penskalaan mengubah data yang mendasarinya, merusak informasi pada nilainya dalam situasi ini"
    ),
    ("questions", 19, "options", 2, "text"): (
        "Itu tidak penting, karena model mempelajari bobot untuk setiap fitur"
    ),
    ("questions", 19, "options", 3, "text"): (
        "Kolom bersatuan besar mendominasi kolom bersatuan kecil tanpa ada yang memilihnya"
    ),
    # --- q21: join ----------------------------------------------------------------------------
    ("questions", 20, "prompt"): (
        "Kamu harus menggabungkan dua tabel: `orders` (satu baris per pesanan) dan `customers` (satu "
        "baris per pelanggan). Setiap pesanan menyebut satu pelanggan. Operasi mana yang melampirkan "
        "atribut pelanggan ke setiap pesanan?"
    ),
    ("questions", 20, "explanation"): (
        "Join pada pengenal pelanggan. Sebuah join mencocokkan baris antar tabel pada kunci yang sama dan "
        "menyatukan kolomnya; memfilter dengan WHERE justru akan memilih baris alih-alih melampirkan "
        "atribut. Ini adalah operasi yang menyusun tabel fitur dari skema yang ternormalisasi."
    ),
    ("questions", 20, "options", 0, "text"): (
        "Join kedua tabel pada kolom pengenal pelanggan yang sama"
    ),
    ("questions", 20, "options", 1, "text"): (
        "Gabungkan kolomnya secara vertikal, menumpuk pesanan ke pelanggan dalam situasi ini"
    ),
    ("questions", 20, "options", 2, "text"): (
        "Filter kedua tabel dengan WHERE, mempertahankan baris yang ada di masing-masing"
    ),
    ("questions", 20, "options", 3, "text"): (
        "Urutkan kedua tabel berdasarkan pelanggan, lalu baca berdampingan"
    ),
    # --- q22: code, top-N above a threshold ---------------------------------------------------
    ("questions", 21, "prompt"): (
        "Tulis kueri yang mengembalikan `name` setiap produk yang harganya secara ketat di atas `?` "
        "(parameter pertama), diurutkan dari harga tertinggi, dan mengembalikan paling banyak `?` baris "
        "(parameter kedua). Editor di bawah menunjukkan skema dan tesnya."
    ),
    ("questions", 21, "explanation"): (
        "Tiga klausa yang bekerja: WHERE hanya menyimpan baris di atas ambang batas, ORDER BY price DESC "
        "menempatkan yang paling mahal di awal, dan LIMIT membatasi jumlahnya. Urutan dua klausa pertama "
        "penting — memfilter sebelum membatasi itulah yang membuat hasilnya menjadi N teratas di atas "
        "ambang batas, bukan N teratas keseluruhan yang difilter setelahnya."
    ),
    ("questions", 21, "code", "instructionsMarkdown"): (
        "Tabelnya adalah `products(name, price)`." + NL +
        "Kembalikan `name` setiap produk dengan `price` yang **LEBIH BESAR secara ketat dari** parameter "
        "pertama, diurutkan dari harga tertinggi ke terendah, dibatasi sampai jumlah baris yang diberikan "
        "parameter kedua." + NL +
        "Tekan **Jalankan tes** untuk menjalankan kueri ini di browser kamu melalui sql.js. Tidak ada "
        "yang berjalan di server."
    ),
    # --- q23: code, GROUP BY and HAVING -------------------------------------------------------
    ("questions", 22, "prompt"): (
        "Tulis kueri yang mengembalikan setiap `category` produk bersama `n`, yaitu jumlah produk di "
        "dalamnya, untuk kategori yang memuat lebih dari `?` produk, diurutkan dari jumlah terbanyak. "
        "Editor di bawah menunjukkan skema dan tesnya."
    ),
    ("questions", 22, "explanation"): (
        "GROUP BY meruntuhkan baris menjadi satu per kategori, COUNT(*) menghitung baris di dalam setiap "
        "grup, dan HAVING memfilter grup-grup itu. HAVING alih-alih WHERE adalah poin utamanya: WHERE "
        "berjalan sebelum pengelompokan dan tidak bisa melihat agregat, sementara HAVING berjalan "
        "setelahnya dan bisa."
    ),
    ("questions", 22, "code", "instructionsMarkdown"): (
        "Tabelnya adalah `products(name, category)`." + NL +
        "Kembalikan satu baris per `category` yang memuat `category` dan `n`, dengan `n` adalah jumlah "
        "produk dalam kategori tersebut. Sertakan hanya kategori yang jumlah produknya **LEBIH BESAR "
        "secara ketat dari** parameter pertama. Urutkan hasilnya berdasarkan `n` secara menurun." + NL +
        "Tekan **Jalankan tes** untuk menjalankan kueri ini di browser kamu melalui sql.js. Tidak ada "
        "yang berjalan di server."
    ),
    # --- q24: code, DISTINCT ----------------------------------------------------------------
    ("questions", 23, "prompt"): (
        "Tulis kueri yang mengembalikan nilai `category` yang unik di tabel `products`, diurutkan secara "
        "alfabetis, dengan mengecualikan kategori apa pun yang disebutkan parameter pertama. Editor di "
        "bawah menunjukkan skema dan tesnya."
    ),
    ("questions", 23, "explanation"): (
        "DISTINCT meruntuhkan nilai duplikat sehingga setiap kategori muncul sekali, klausa WHERE "
        "menghapus yang dikecualikan, dan ORDER BY menempatkan sisanya dalam urutan alfabetis. Melakukan "
        "DISTINCT dan pengecualian dalam satu lintasan itulah yang menjaga hasilnya tetap berupa daftar "
        "kategori yang bersih alih-alih daftar setiap baris produk."
    ),
    ("questions", 23, "code", "instructionsMarkdown"): (
        "Tabelnya adalah `products(name, category)`, dan berisi satu baris per produk, sehingga sebuah "
        "kategori berulang setiap kali memuat beberapa produk." + NL +
        "Kembalikan nilai `category` yang **unik**, diurutkan secara alfabetis, dengan mengecualikan "
        "kategori yang namanya diberikan sebagai parameter pertama." + NL +
        "Tekan **Jalankan tes** untuk menjalankan kueri ini di browser kamu melalui sql.js. Tidak ada "
        "yang berjalan di server."
    ),
    # --- q25: code, BETWEEN -------------------------------------------------------------------
    ("questions", 24, "prompt"): (
        "Tulis kueri yang mengembalikan `name` dan `price` untuk setiap produk yang harganya berada dalam "
        "rentang inklusif, diurutkan dari yang paling murah, dengan batas bawah dan batas atas adalah "
        "parameter pertama dan kedua. Editor di bawah menunjukkan skema dan tesnya."
    ),
    ("questions", 24, "explanation"): (
        "BETWEEN bersifat inklusif di kedua ujungnya, sehingga produk yang harganya tepat di salah satu "
        "batas tetap disertakan — itulah yang biasanya berarti \"dalam rentang\". ORDER BY price secara "
        "menaik memberikan yang paling murah lebih dahulu, tanpa LIMIT, sehingga seluruh himpunan yang "
        "memenuhi syarat dikembalikan."
    ),
    ("questions", 24, "code", "instructionsMarkdown"): (
        "Tabelnya adalah `products(name, price)`." + NL +
        "Kembalikan `name` dan `price` untuk setiap produk yang harganya berada **dalam rentang "
        "inklusif** yang diberikan parameter pertama (bawah) dan parameter kedua (atas). Urutkan "
        "hasilnya berdasarkan `price` secara menaik. Sertakan kedua ujungnya." + NL +
        "Tekan **Jalankan tes** untuk menjalankan kueri ini di browser kamu melalui sql.js. Tidak ada "
        "yang berjalan di server."
    ),
}

# Where each Indonesian field goes: the key it translates, and the name of the field it adds.
PAIRS = {"title": "titleId", "scenarioMarkdown": "scenarioMarkdownId", "prompt": "promptId",
         "explanation": "explanationId", "text": "textId",
         "instructionsMarkdown": "instructionsMarkdownId", "rubricMarkdown": "rubricMarkdownId"}


def localise(source: dict, translations: dict, used: set) -> dict:
    """Rebuild the level with each `Id` field placed immediately after the field it translates."""
    result: dict = {}
    for key, value in source.items():
        if key == "caseStudy":
            case: dict = {}
            for case_key, case_value in value.items():
                case[case_key] = case_value
                if case_key in ("title", "scenarioMarkdown"):
                    path = ("caseStudy", case_key)
                    case[PAIRS[case_key]] = translations[path]
                    used.add(path)
            result[key] = case
        elif key == "questions":
            questions = []
            for index, question in enumerate(value):
                rebuilt: dict = {}
                for q_key, q_value in question.items():
                    if q_key == "options":
                        options = []
                        for option_index, option in enumerate(q_value):
                            new_option: dict = {}
                            for option_key, option_value in option.items():
                                new_option[option_key] = option_value
                                if option_key == "text":
                                    path = ("questions", index, "options", option_index, "text")
                                    new_option["textId"] = translations[path]
                                    used.add(path)
                            options.append(new_option)
                        rebuilt[q_key] = options
                    elif q_key in ("code", "essay"):
                        spec: dict = {}
                        for spec_key, spec_value in q_value.items():
                            spec[spec_key] = spec_value
                            if spec_key in ("instructionsMarkdown", "rubricMarkdown"):
                                path = ("questions", index, q_key, spec_key)
                                spec[PAIRS[spec_key]] = translations[path]
                                used.add(path)
                        rebuilt[q_key] = spec
                    else:
                        rebuilt[q_key] = q_value
                        if q_key in ("prompt", "explanation"):
                            path = ("questions", index, q_key)
                            rebuilt[PAIRS[q_key]] = translations[path]
                            used.add(path)
                questions.append(rebuilt)
            result[key] = questions
        else:
            result[key] = value
    return result


def main() -> int:
    original_text = SRC.read_text(encoding="utf-8")
    source = json.loads(original_text)

    used: set = set()
    translated = localise(source, TRANSLATIONS, used)

    if used != set(TRANSLATIONS):
        raise SystemExit(f"unused translations: {sorted(set(TRANSLATIONS) - used)}")

    # An `Id` field that merely repeats its English source is not a translation.
    for question_index, question in enumerate(translated["questions"]):
        for english_key, indonesian_key in PAIRS.items():
            if english_key in question and question.get(indonesian_key, "").strip() == \
                    str(question[english_key]).strip():
                raise SystemExit(f"q{question_index + 1}: {indonesian_key} identical to English")
        for option_index, option in enumerate(question.get("options", [])):
            if option["textId"].strip() == option["text"].strip():
                raise SystemExit(f"q{question_index + 1} option{option_index + 1}: untranslated")
        for spec_key in ("code", "essay"):
            spec = question.get(spec_key, {})
            for english_key, indonesian_key in PAIRS.items():
                if english_key in spec and spec[indonesian_key].strip() == str(spec[english_key]).strip():
                    raise SystemExit(f"q{question_index + 1} {spec_key}: untranslated")
    case = translated["caseStudy"]
    for english_key, indonesian_key in PAIRS.items():
        if english_key in case and case[indonesian_key].strip() == str(case[english_key]).strip():
            raise SystemExit(f"caseStudy.{indonesian_key}: untranslated")

    output_text = json.dumps(translated, indent=2, ensure_ascii=False) + chr(10)
    with open(DST, "w", encoding="utf-8", newline=chr(10)) as handle:
        handle.write(output_text)

    # Byte-level proof that nothing but the inserted `Id` lines changed: delete those lines, undo the
    # single comma they force (caseStudy.scenarioMarkdown was the last key of its object), and the
    # remainder must equal the original file exactly.
    original_lines = original_text.split(chr(10))
    kept = []
    for line in output_text.split(chr(10)):
        stripped = line.strip()
        if any(stripped.startswith('"' + name + '":') for name in ID_KEYS):
            continue
        kept.append(line)

    if len(kept) != len(original_lines):
        raise SystemExit(f"line count {len(kept)} != {len(original_lines)}")
    comma_fixes = 0
    for index, (before, after) in enumerate(zip(original_lines, kept)):
        if before == after:
            continue
        if after == before + "," and before.strip().startswith('"scenarioMarkdown"'):
            comma_fixes += 1
            continue
        raise SystemExit(f"line {index + 1} changed beyond the Id fields:\n  {before!r}\n  {after!r}")
    if comma_fixes != 1:
        raise SystemExit(f"expected exactly one comma fix, found {comma_fixes}")

    print(f"wrote {DST.name}: {len(TRANSLATIONS)} translation(s)")
    print("byte-level check: removing the Id lines and their one comma restores the original exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
