/* NETWORK-DRAWN certificate (ADR-0136/0139): a person's credential as a paper certificate — on screen, as an A4 PDF, printed.
   The QR code opens the network's public check (/embed/verify?id=…): the issuer's signature, and whether it is revoked.
   Data comes from the page (<script type="application/json" id="cert-data">), never from the URL. Needs
   /demo/vendor/qrcode-generator-1.4.4.min.js and /demo/vendor/jspdf-2.5.1.umd.min.js. */
(function(){
  var D = JSON.parse(document.getElementById('cert-data').textContent);
  function qr(){ var x = qrcode(0, 'M'); x.addData(D.verify_url); x.make(); return x; }
  function asc(s){ return String(s == null ? '' : s).replace(/[–—]/g, '-').replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/₹/g, 'Rs ').replace(/[^\x20-\xff]/g, ''); }
  function build(){
    var doc = new jspdf.jsPDF({orientation: 'portrait', unit: 'mm', format: 'a4'}), W = 210, C = W / 2;
    var navy = [14, 42, 71], gold = [176, 141, 62], ink = [21, 32, 43], soft = [95, 113, 131];
    doc.setDrawColor.apply(doc, navy); doc.setLineWidth(1.2); doc.rect(9, 9, 192, 279);
    doc.setDrawColor.apply(doc, gold); doc.setLineWidth(0.4); doc.rect(12.5, 12.5, 185, 272);
    doc.setFillColor.apply(doc, navy); doc.circle(C, 33, 11, 'F'); doc.setDrawColor.apply(doc, gold); doc.setLineWidth(0.8); doc.circle(C, 33, 9);
    doc.setTextColor(255, 255, 255); doc.setFont('helvetica', 'bold'); doc.setFontSize(15); doc.text(asc(D.seal), C, 38.2, {align: 'center'});
    doc.setTextColor.apply(doc, soft); doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5);
    doc.text(asc(D.network_line).toUpperCase(), C, 53, {align: 'center', charSpace: 0.6});
    doc.setTextColor.apply(doc, navy); doc.setFont('times', 'bold'); doc.setFontSize(24);
    var tl = doc.splitTextToSize(asc(D.title), 165); doc.text(tl, C, 70, {align: 'center'});
    var y = 70 + tl.length * 9 + 4;
    doc.setDrawColor.apply(doc, gold); doc.setLineWidth(0.5); doc.line(C - 30, y - 4, C + 30, y - 4);
    doc.setTextColor.apply(doc, soft); doc.setFont('times', 'italic'); doc.setFontSize(13); doc.text('This is to certify that', C, y + 6, {align: 'center'});
    doc.setTextColor.apply(doc, ink); doc.setFont('times', 'bold'); doc.setFontSize(22); doc.text(asc(D.name || '-'), C, y + 19, {align: 'center'});
    doc.setTextColor.apply(doc, soft); doc.setFont('times', 'italic'); doc.setFontSize(12);
    var by = doc.splitTextToSize(asc(D.statement), 160); doc.text(by, C, y + 30, {align: 'center'});
    y = y + 30 + by.length * 6 + 8;
    doc.setFont('helvetica', 'normal'); doc.setFontSize(10.5);
    (D.rows || []).slice(0, 14).forEach(function(r){
      doc.setDrawColor(228, 233, 239); doc.setLineWidth(0.2); doc.line(30, y + 2.5, 180, y + 2.5);
      doc.setTextColor.apply(doc, soft); doc.setFont('helvetica', 'normal'); doc.text(asc(r[0]), 32, y);
      doc.setTextColor.apply(doc, ink); doc.setFont('helvetica', 'bold'); doc.text(doc.splitTextToSize(asc(r[1]), 90)[0], 95, y);
      y += 8.5; });
    var q = qr(), n = q.getModuleCount(), size = 42, cell = size / n, x0 = 150, y0 = 222;
    doc.setFillColor(255, 255, 255); doc.rect(x0 - 2, y0 - 2, size + 4, size + 4, 'F');
    doc.setFillColor.apply(doc, ink); for (var i = 0; i < n; i++) for (var j = 0; j < n; j++) if (q.isDark(i, j)) doc.rect(x0 + j * cell, y0 + i * cell, cell + 0.01, cell + 0.01, 'F');
    doc.setFontSize(7.5); doc.setTextColor.apply(doc, soft); doc.setFont('helvetica', 'normal');
    doc.text('Scan to check it is genuine', x0 + size / 2, y0 + size + 5, {align: 'center'});
    doc.setFontSize(9); doc.setTextColor.apply(doc, ink); doc.setFont('helvetica', 'bold');
    doc.text('Issued by ' + asc(D.issuer), 25, 230); doc.setFont('helvetica', 'normal'); doc.setTextColor.apply(doc, soft);
    doc.text('Issued on ' + asc(D.issued_on) + '   Ref. ' + asc(D.ref), 25, 236);
    doc.setFontSize(7.8);
    doc.text(doc.splitTextToSize('Digitally signed by the issuer. This paper copy is valid only while the check behind the QR code says "Genuine and current". Check it at:', 115), 25, 246);
    doc.setTextColor.apply(doc, navy); doc.text(doc.splitTextToSize(asc(D.verify_url), 115), 25, 258);
    return doc; }
  window.__aosCertificatePdf = build;                   // for testing; the page uses the buttons
  var box = document.getElementById('qr');
  if (box && window.qrcode) { box.innerHTML = qr().createSvgTag(4, 0); }
  var b = document.getElementById('pdf');
  if (b) b.onclick = function(){ try { build().save(D.filename); b.textContent = 'Downloaded ✓'; } catch (e) { b.textContent = 'PDF failed: ' + e.message; } };
  var p = document.getElementById('print'); if (p) p.onclick = function(){ window.print(); };
})();
