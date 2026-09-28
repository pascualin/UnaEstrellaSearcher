(function () {
  function wrapLines(ctx, text, maxWidth) {
    const paragraphs = String(text || "").split(/\n+/);
    const lines = [];
    paragraphs.forEach((paragraph) => {
      const words = paragraph.trim().split(/\s+/).filter(Boolean);
      if (!words.length) return;
      let line = words.shift();
      words.forEach((word) => {
        const candidate = `${line} ${word}`;
        if (ctx.measureText(candidate).width <= maxWidth) {
          line = candidate;
        } else {
          lines.push(line);
          line = word;
        }
      });
      lines.push(line);
    });
    return lines.length ? lines : [""];
  }

  function drawLines(ctx, lines, x, y, lineHeight, color) {
    ctx.fillStyle = color;
    lines.forEach((line, index) => ctx.fillText(line, x, y + index * lineHeight));
    return y + lines.length * lineHeight;
  }

  function initials(value) {
    const words = String(value || "?").trim().split(/\s+/).filter(Boolean);
    if (!words.length) return "?";
    return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase();
  }

  function avatarHue(value) {
    let hash = 0;
    for (const char of String(value || "")) {
      hash = ((hash << 5) - hash) + char.charCodeAt(0);
      hash |= 0;
    }
    return Math.abs(hash) % 360;
  }

  function drawAvatar(ctx, x, y, size, value, light) {
    const hue = avatarHue(value);
    ctx.beginPath();
    ctx.arc(x + size / 2, y + size / 2, size / 2, 0, Math.PI * 2);
    ctx.fillStyle = light ? `hsl(${hue} 46% 88%)` : `hsl(${hue} 52% 48%)`;
    ctx.fill();
    ctx.fillStyle = light ? "#3c4043" : "#ffffff";
    ctx.font = `700 ${Math.round(size * 0.34)}px "Outfit", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(initials(value), x + size / 2, y + size / 2 + 2);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  function drawStars(ctx, x, y, rating) {
    const filled = Math.max(0, Math.min(5, Number(rating) || 0));
    ctx.font = '700 28px "Outfit", sans-serif';
    for (let index = 0; index < 5; index += 1) {
      ctx.fillStyle = index < filled ? "#fbbc04" : "#dadce0";
      ctx.fillText("★", x + index * 32, y);
    }
  }

  async function buildPng(place, review) {
    if (document.fonts?.ready) await document.fonts.ready;

    const width = 980;
    const padding = 52;
    const contentWidth = width - padding * 2;
    const placeName = String(place.place_name || "Sitio").trim();
    const placeCategory = String(place.place_category || "Lugar").trim();
    const placeAddress = String(place.place_address || "Sin dirección").trim();
    const reviewerName = String(review.reviewer_name || "Anónimo").trim();
    const reviewText = String(review.review_text || "(sin texto)").trim();
    const ownerReply = String(review.owner_reply_text || "").trim();
    const ownerReplyDate = String(review.owner_reply_date || "").trim();

    const canvas = document.createElement("canvas");
    const estimate = canvas.getContext("2d");
    estimate.font = '700 48px "Outfit", sans-serif';
    const placeLines = wrapLines(estimate, placeName, contentWidth - 210);
    estimate.font = '400 25px "Outfit", sans-serif';
    const addressLines = wrapLines(estimate, placeAddress, contentWidth - 210);
    estimate.font = '400 30px "Outfit", sans-serif';
    const reviewLines = wrapLines(estimate, reviewText, contentWidth);
    estimate.font = '400 28px "Outfit", sans-serif';
    const replyLines = ownerReply ? wrapLines(estimate, ownerReply, contentWidth - 60) : [];

    const placeHeight = Math.max(318, 88 + placeLines.length * 56 + addressLines.length * 34 + 74);
    const replyHeight = ownerReply ? 126 + replyLines.length * 40 + (ownerReplyDate ? 48 : 0) : 0;
    const height = placeHeight + 300 + reviewLines.length * 44 + replyHeight + 90;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");

    ctx.fillStyle = "#f5f5f5";
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = "#202124";
    ctx.font = '700 48px "Outfit", sans-serif';
    drawLines(ctx, placeLines, padding, 86, 56, "#202124");
    ctx.font = '400 26px "Outfit", sans-serif';
    ctx.fillStyle = "#5f6368";
    ctx.fillText(placeCategory, padding, 164 + (placeLines.length - 1) * 56);
    ctx.font = '400 25px "Outfit", sans-serif';
    drawLines(ctx, addressLines, padding, 212 + (placeLines.length - 1) * 56, 34, "#5f6368");
    drawAvatar(ctx, width - padding - 152, 38, 152, placeName, false);

    ctx.strokeStyle = "#d2d2d2";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, placeHeight);
    ctx.lineTo(width, placeHeight);
    ctx.stroke();

    let y = placeHeight + 74;
    ctx.font = '400 25px "Outfit", sans-serif';
    ctx.fillStyle = "#5f6368";
    ctx.fillText("No se verificaron las opiniones", padding, y);
    y += 52;
    drawAvatar(ctx, padding, y, 82, reviewerName, true);
    ctx.font = '700 34px "Outfit", sans-serif';
    ctx.fillStyle = "#202124";
    ctx.fillText(reviewerName, padding + 114, y + 52);
    y += 136;
    drawStars(ctx, padding, y, review.rating);
    ctx.font = '500 26px "Outfit", sans-serif';
    ctx.fillStyle = "#5f6368";
    ctx.fillText(String(review.date || ""), padding + 230, y);
    y += 66;
    ctx.font = '400 30px "Outfit", sans-serif';
    y = drawLines(ctx, reviewLines, padding, y, 44, "#202124");

    if (ownerReply) {
      y += 34;
      const boxHeight = replyHeight;
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#e1e3e1";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.roundRect(padding, y, contentWidth, boxHeight, 24);
      ctx.fill();
      ctx.stroke();
      ctx.font = '700 28px "Outfit", sans-serif';
      ctx.fillStyle = "#202124";
      ctx.fillText("Respuesta del propietario", padding + 30, y + 54);
      ctx.font = '400 28px "Outfit", sans-serif';
      const replyEnd = drawLines(ctx, replyLines, padding + 30, y + 106, 40, "#202124");
      if (ownerReplyDate) {
        ctx.font = '500 24px "Outfit", sans-serif';
        ctx.fillStyle = "#5f6368";
        ctx.fillText(ownerReplyDate, padding + 30, replyEnd + 20);
      }
    }

    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("No se pudo generar la captura.")), "image/png");
    });
  }

  window.ReviewCapture = { buildPng };
  document.documentElement.dataset.reviewCaptureReady = "true";
})();
