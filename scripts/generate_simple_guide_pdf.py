import os
import sys
import zlib

# Standard Helvetica character widths in 1/1000 of an em
HELVETICA_WIDTHS = {
    ' ': 278, '!': 278, '"': 355, '#': 556, '$': 556, '%': 889, '&': 667, "'": 191,
    '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333, '.': 278, '/': 278,
    '0': 556, '1': 556, '2': 556, '3': 556, '4': 556, '5': 556, '6': 556, '7': 556,
    '8': 556, '9': 556, ':': 278, ';': 278, '<': 584, '=': 584, '>': 584, '?': 556,
    '@': 1015, 'A': 667, 'B': 667, 'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778,
    'H': 722, 'I': 278, 'J': 500, 'K': 667, 'L': 556, 'M': 833, 'N': 722, 'O': 778,
    'P': 667, 'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 278, '\\': 278, ']': 278, '^': 469, '_': 556,
    '`': 333, 'a': 556, 'b': 556, 'c': 500, 'd': 556, 'e': 556, 'f': 278, 'g': 556,
    'h': 556, 'i': 222, 'j': 222, 'k': 500, 'l': 222, 'm': 833, 'n': 556, 'o': 556,
    'p': 556, 'q': 556, 'r': 333, 's': 500, 't': 278, 'u': 556, 'v': 500, 'w': 722,
    'x': 500, 'y': 500, 'z': 500, '{': 334, '|': 260, '}': 334, '~': 584,
    '—': 1000, '–': 500, '“': 500, '”': 500, '‘': 250, '’': 250, '•': 500, '→': 800
}

def text_width(text, font_size, is_bold=False):
    factor = 1.08 if is_bold else 1.0
    w = sum(HELVETICA_WIDTHS.get(c, 550) for c in text)
    return (w / 1000.0) * font_size * factor

def escape_pdf(text):
    text = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    # replace non-ascii chars with clean ASCII representations
    replacements = {
        '—': '--', '–': '-', '→': '->', '•': '*', '“': '"', '”': '"', '‘': "'", '’': "'",
        '≥': '>=', '≤': '<=', '±': '+/-', '×': 'x', '…': '...', '′': "'", '″': '"', 'pp': 'pp'
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode('latin1', 'replace').decode('latin1')

def wrap_text(text, max_w, font_size, is_bold=False):
    words = text.split(' ')
    lines = []
    curr = []
    for w in words:
        test = ' '.join(curr + [w]) if curr else w
        if text_width(test, font_size, is_bold) <= max_w:
            curr.append(w)
        else:
            if curr:
                lines.append(' '.join(curr))
                curr = [w]
            else:
                lines.append(w)
                curr = []
    if curr:
        lines.append(' '.join(curr))
    return lines

class PDFDoc:
    def __init__(self, filename="PayLens_Explained_Simply.pdf"):
        self.filename = filename
        self.page_w = 612.0
        self.page_h = 792.0
        self.margin_x = 54.0
        self.margin_top = 54.0
        self.margin_bottom = 54.0
        self.content_w = self.page_w - (2 * self.margin_x)  # 504 pt
        
        self.pages = []
        self.current_stream = []
        self.cursor_y = self.page_h - self.margin_top
        self.page_num = 0
        self.total_pages = 1
        
        # Color palette
        self.c_primary = (0.06, 0.20, 0.38)     # Deep Navy / Pine Slate
        self.c_teal = (0.05, 0.46, 0.43)        # Teal / Pine Green
        self.c_text = (0.12, 0.16, 0.23)        # Dark Charcoal
        self.c_muted = (0.35, 0.40, 0.48)       # Slate Gray
        self.c_bg_light = (0.95, 0.96, 0.98)    # Light gray background
        self.c_card_bg = (0.97, 0.98, 0.99)     # Soft card background
        self.c_card_border = (0.80, 0.84, 0.90) # Soft card border
        self.c_accent_green = (0.08, 0.55, 0.35)# Success Green
        self.c_accent_red = (0.80, 0.20, 0.20)  # Alert Red
        
        self.start_new_page(is_first=True)

    def start_new_page(self, is_first=False):
        if not is_first:
            self.pages.append(self.current_stream)
            self.current_stream = []
        self.page_num += 1
        self.cursor_y = self.page_h - self.margin_top
        
        # Running header (for subsequent pages)
        if not is_first:
            self.cursor_y -= 10
            # Header text
            self.draw_text("PayLens: Plain-English Guide to Payment Intelligence", 
                           self.margin_x, self.cursor_y, font="F3", size=8.5, color=self.c_muted)
            self.draw_text("Pine Labs Workspace", 
                           self.page_w - self.margin_x - text_width("Pine Labs Workspace", 8.5), 
                           self.cursor_y, font="F1", size=8.5, color=self.c_muted)
            self.cursor_y -= 6
            # Header rule line
            self.draw_line(self.margin_x, self.cursor_y, self.page_w - self.margin_x, self.cursor_y, 
                           width=0.5, color=(0.85, 0.88, 0.92))
            self.cursor_y -= 20

    def check_space(self, required_h):
        if self.cursor_y - required_h < self.margin_bottom + 25:
            self.start_new_page()

    def draw_line(self, x1, y1, x2, y2, width=1.0, color=(0, 0, 0)):
        cmd = f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S\n"
        self.current_stream.append(cmd)

    def draw_rect(self, x, y, w, h, fill=None, border=None, border_w=1.0):
        cmd = ""
        if fill:
            cmd += f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg "
        if border:
            cmd += f"{border[0]:.3f} {border[1]:.3f} {border[2]:.3f} RG {border_w:.2f} w "
        cmd += f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re "
        if fill and border:
            cmd += "B\n"
        elif fill:
            cmd += "f\n"
        elif border:
            cmd += "S\n"
        self.current_stream.append(cmd)

    def draw_text(self, text, x, y, font="F1", size=10.0, color=(0, 0, 0)):
        safe_text = escape_pdf(text)
        cmd = (f"BT\n"
               f"/{font} {size:.2f} Tf\n"
               f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg\n"
               f"{x:.2f} {y:.2f} Td\n"
               f"({safe_text}) Tj\n"
               f"ET\n")
        self.current_stream.append(cmd)

    def add_title_block(self):
        # Decorative top badge
        badge_text = "PINE LABS WORKSPACE OVERVIEW"
        badge_w = text_width(badge_text, 8, is_bold=True) + 16
        self.draw_rect(self.margin_x, self.cursor_y - 14, badge_w, 16, 
                       fill=(0.90, 0.94, 0.98), border=(0.20, 0.40, 0.70), border_w=0.8)
        self.draw_text(badge_text, self.margin_x + 8, self.cursor_y - 11, font="F2", size=8, color=self.c_primary)
        self.cursor_y -= 30

        # Main Title
        title = "PayLens: Plain-English Project Guide"
        self.draw_text(title, self.margin_x, self.cursor_y - 18, font="F2", size=22, color=self.c_primary)
        self.cursor_y -= 30

        # Subtitle
        sub = "How this Payment Intelligence Analyst Works, Why It Was Built, and What Every Folder Does"
        self.draw_text(sub, self.margin_x, self.cursor_y - 10, font="F3", size=10.5, color=self.c_muted)
        self.cursor_y -= 18

        # Decorative bar
        self.draw_rect(self.margin_x, self.cursor_y - 3, self.content_w, 3, fill=self.c_teal)
        self.cursor_y -= 22

    def add_section(self, num_str, title):
        self.check_space(55)
        self.cursor_y -= 8
        full_title = f"{num_str}. {title.upper()}"
        # Background bar
        bar_h = 22
        self.draw_rect(self.margin_x, self.cursor_y - bar_h, self.content_w, bar_h, fill=(0.93, 0.95, 0.97))
        self.draw_rect(self.margin_x, self.cursor_y - bar_h, 4, bar_h, fill=self.c_teal)
        self.draw_text(full_title, self.margin_x + 12, self.cursor_y - bar_h + 6, font="F2", size=11, color=self.c_primary)
        self.cursor_y -= (bar_h + 12)

    def add_subsection(self, title):
        self.check_space(35)
        self.cursor_y -= 4
        self.draw_text(title, self.margin_x, self.cursor_y - 11, font="F2", size=11, color=self.c_teal)
        self.cursor_y -= 18

    def add_paragraph(self, text, bold_prefix="", italic=False):
        font = "F3" if italic else "F1"
        size = 9.5
        line_h = 13.5
        
        full_str = (bold_prefix + " " + text).strip() if bold_prefix else text
        lines = wrap_text(full_str, self.content_w, size)
        
        self.check_space(len(lines) * line_h + 8)
        
        first = True
        for line in lines:
            if first and bold_prefix:
                # print prefix bold then rest
                bp_w = text_width(bold_prefix + " ", size, is_bold=True)
                rest = line[len(bold_prefix):].lstrip()
                self.draw_text(bold_prefix + " ", self.margin_x, self.cursor_y - size, font="F2", size=size, color=self.c_text)
                self.draw_text(rest, self.margin_x + bp_w, self.cursor_y - size, font=font, size=size, color=self.c_text)
                first = False
            else:
                self.draw_text(line, self.margin_x, self.cursor_y - size, font=font, size=size, color=self.c_text)
            self.cursor_y -= line_h
        self.cursor_y -= 5

    def add_bullet(self, title, text):
        size = 9.5
        line_h = 13.5
        indent = 16.0
        bullet_w = self.content_w - indent
        
        full_text = f"{title}: {text}" if title else text
        lines = wrap_text(full_text, bullet_w, size)
        
        self.check_space(len(lines) * line_h + 6)
        
        # Draw bullet symbol
        self.draw_text("*", self.margin_x + 4, self.cursor_y - size, font="F2", size=11, color=self.c_teal)
        
        first = True
        for line in lines:
            if first and title:
                t_str = f"{title}: "
                t_w = text_width(t_str, size, is_bold=True)
                rest = line[len(t_str):] if line.startswith(t_str) else line
                self.draw_text(t_str, self.margin_x + indent, self.cursor_y - size, font="F2", size=size, color=self.c_text)
                self.draw_text(rest, self.margin_x + indent + t_w, self.cursor_y - size, font="F1", size=size, color=self.c_text)
                first = False
            else:
                self.draw_text(line, self.margin_x + indent, self.cursor_y - size, font="F1", size=size, color=self.c_text)
            self.cursor_y -= line_h
        self.cursor_y -= 4

    def add_callout(self, text, title="KEY INTUITION", style="info"):
        size = 9.0
        line_h = 12.8
        pad_x = 12.0
        pad_y = 10.0
        box_w = self.content_w
        
        bg_col = (0.95, 0.98, 0.96) if style == "success" else ((0.99, 0.96, 0.96) if style == "warning" else (0.94, 0.96, 0.99))
        border_col = self.c_accent_green if style == "success" else (self.c_accent_red if style == "warning" else self.c_primary)
        
        lines = wrap_text(text, box_w - (2 * pad_x), size)
        total_h = (len(lines) * line_h) + (2 * pad_y) + (14 if title else 0)
        
        self.check_space(total_h + 10)
        
        # Draw background and thick left border
        self.draw_rect(self.margin_x, self.cursor_y - total_h, box_w, total_h, fill=bg_col, border=(0.85, 0.88, 0.92), border_w=0.6)
        self.draw_rect(self.margin_x, self.cursor_y - total_h, 4.5, total_h, fill=border_col)
        
        cur_y = self.cursor_y - pad_y
        if title:
            self.draw_text(title, self.margin_x + pad_x, cur_y - 8.5, font="F2", size=8.5, color=border_col)
            cur_y -= 14
            
        for line in lines:
            self.draw_text(line, self.margin_x + pad_x, cur_y - size, font="F1", size=size, color=self.c_text)
            cur_y -= line_h
            
        self.cursor_y -= (total_h + 10)

    def add_table(self, headers, rows, col_widths=None):
        if not col_widths:
            col_widths = [self.content_w / len(headers)] * len(headers)
        
        row_h = 18.0
        header_h = 20.0
        total_h = header_h + (len(rows) * row_h)
        
        self.check_space(total_h + 12)
        
        # Header background
        self.draw_rect(self.margin_x, self.cursor_y - header_h, self.content_w, header_h, fill=self.c_primary)
        
        # Header text
        cur_x = self.margin_x
        for i, h in enumerate(headers):
            self.draw_text(h, cur_x + 6, self.cursor_y - header_h + 6, font="F2", size=8.5, color=(1, 1, 1))
            cur_x += col_widths[i]
            
        self.cursor_y -= header_h
        
        # Rows
        for r_idx, row in enumerate(rows):
            bg = (0.98, 0.98, 0.99) if r_idx % 2 == 0 else (0.93, 0.94, 0.96)
            self.draw_rect(self.margin_x, self.cursor_y - row_h, self.content_w, row_h, fill=bg, border=(0.88, 0.90, 0.93), border_w=0.4)
            cur_x = self.margin_x
            for c_idx, cell in enumerate(row):
                # Is bold if first col
                is_b = (c_idx == 0 or cell.endswith("%") or "MIX" in cell or "RATE" in cell)
                font = "F2" if is_b else "F1"
                self.draw_text(str(cell), cur_x + 6, self.cursor_y - row_h + 5, font=font, size=8.5, color=self.c_text)
                cur_x += col_widths[c_idx]
            self.cursor_y -= row_h
            
        self.cursor_y -= 10

    def add_flow_diagram(self, steps):
        # Visual horizontal/vertical pipeline flow
        step_h = 26.0
        gap = 6.0
        total_h = (len(steps) * (step_h + gap))
        
        self.check_space(total_h + 10)
        
        for idx, (label, desc, tag) in enumerate(steps):
            y_box = self.cursor_y - step_h
            # Step box
            self.draw_rect(self.margin_x, y_box, self.content_w, step_h, fill=(0.96, 0.97, 0.99), border=(0.82, 0.86, 0.92), border_w=0.6)
            # Step number pill
            self.draw_rect(self.margin_x + 4, y_box + 4, 22, 18, fill=self.c_teal)
            self.draw_text(str(idx + 1), self.margin_x + 11, y_box + 9, font="F2", size=9, color=(1, 1, 1))
            
            # Step label & description
            self.draw_text(label, self.margin_x + 34, y_box + 9, font="F2", size=9, color=self.c_primary)
            self.draw_text(f"-- {desc}", self.margin_x + 34 + text_width(label + " ", 9, is_bold=True), y_box + 9, font="F1", size=8.5, color=self.c_muted)
            
            # Optional tag on right
            if tag:
                tag_w = text_width(tag, 7.5, is_bold=True) + 10
                self.draw_rect(self.page_w - self.margin_x - tag_w - 6, y_box + 5, tag_w, 16, fill=(0.88, 0.92, 0.98), border=(0.70, 0.78, 0.90), border_w=0.5)
                self.draw_text(tag, self.page_w - self.margin_x - tag_w - 1, y_box + 9, font="F2", size=7.5, color=self.c_primary)
            
            self.cursor_y -= (step_h + gap)
        self.cursor_y -= 6

    def finalize(self):
        # Push last page
        self.pages.append(self.current_stream)
        total_p = len(self.pages)
        
        # Add page footers to all pages
        for p_idx, page_stream in enumerate(self.pages):
            p_num = p_idx + 1
            footer_line_y = self.margin_bottom + 12
            # subtle line
            cmd_line = f"0.850 0.880 0.920 RG 0.50 w {self.margin_x:.2f} {footer_line_y:.2f} m {self.page_w - self.margin_x:.2f} {footer_line_y:.2f} l S\n"
            page_stream.append(cmd_line)
            
            # Left text
            left_t = "PayLens: Plain-English Project Guide | Pine Labs Workspace"
            cmd_left = (f"BT\n/F1 8.00 Tf\n0.400 0.450 0.520 rg\n{self.margin_x:.2f} {self.margin_bottom:.2f} Td\n({escape_pdf(left_t)}) Tj\nET\n")
            page_stream.append(cmd_left)
            
            # Right page number
            p_text = f"Page {p_num} of {total_p}"
            p_w = text_width(p_text, 8.0)
            p_x = self.page_w - self.margin_x - p_w
            cmd_right = (f"BT\n/F2 8.00 Tf\n0.200 0.300 0.450 rg\n{p_x:.2f} {self.margin_bottom:.2f} Td\n({escape_pdf(p_text)}) Tj\nET\n")
            page_stream.append(cmd_right)

        # Build PDF objects
        objects = []
        
        # Obj 1: Catalog
        objects.append("<< /Type /Catalog /Pages 2 0 R >>")
        
        # Obj 2: Pages container
        kids_refs = [f"{3 + i*2} 0 R" for i in range(total_p)]
        kids_str = " ".join(kids_refs)
        objects.append(f"<< /Type /Pages /Kids [{kids_str}] /Count {total_p} >>")
        
        # Font objects
        # 3 + total_p*2 will be fonts
        font_base_idx = 3 + total_p * 2
        
        for i, page_stream in enumerate(self.pages):
            page_obj_id = 3 + i * 2
            content_obj_id = page_obj_id + 1
            
            # Page object
            page_dict = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.page_w:.2f} {self.page_h:.2f}] "
                         f"/Contents {content_obj_id} 0 R "
                         f"/Resources << /Font << "
                         f"/F1 {font_base_idx} 0 R "
                         f"/F2 {font_base_idx+1} 0 R "
                         f"/F3 {font_base_idx+2} 0 R "
                         f"/F4 {font_base_idx+3} 0 R "
                         f">> >> >>")
            objects.append(page_dict)
            
            # Content stream
            raw_content = "".join(page_stream).encode('latin1')
            comp = zlib.compress(raw_content)
            stream_dict = f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n"
            objects.append((stream_dict, comp))
            
        # Fonts: F1: Helvetica, F2: Helvetica-Bold, F3: Helvetica-Oblique, F4: Helvetica-BoldOblique
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>")
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-BoldOblique >>")
        
        # Compile PDF binary
        out = bytearray()
        out.extend(b"%PDF-1.4\n")
        
        xref_offsets = []
        xref_offsets.append(0) # object 0
        
        for i, obj in enumerate(objects):
            obj_num = i + 1
            offset = len(out)
            xref_offsets.append(offset)
            out.extend(f"{obj_num} 0 obj\n".encode('latin1'))
            if isinstance(obj, tuple):
                header_str, raw_bytes = obj
                out.extend(header_str.encode('latin1'))
                out.extend(raw_bytes)
                out.extend(b"\nendstream\nendobj\n")
            else:
                out.extend(obj.encode('latin1'))
                out.extend(b"\nendobj\n")
                
        # XRef table
        xref_start = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode('latin1'))
        out.extend(b"0000000000 65535 f \n")
        for off in xref_offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode('latin1'))
            
        # Trailer
        trailer = (f"trailer\n"
                   f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                   f"startxref\n{xref_start}\n%%EOF\n")
        out.extend(trailer.encode('latin1'))
        
        with open(self.filename, 'wb') as f:
            f.write(out)
            
        print(f"Successfully generated {self.filename} ({len(out)} bytes, {total_p} pages).")


def build_guide():
    pdf = PDFDoc(filename="PayLens_Explained_Simply.pdf")
    pdf.add_title_block()
    
    # Section 1: Executive Summary
    pdf.add_section("1", "Executive Summary: What is this Project?")
    pdf.add_paragraph(
        "PayLens is an intelligent business analyst built specifically for digital payment systems (such as UPI, Cards, and Netbanking). "
        "It solves a classic dilemma in high-tech payment companies: when payment success rates drop, executive managers want clear answers, "
        "engineers want exact diagnostic data, and nobody wants an AI assistant that hallucinates or invents fake numbers."
    )
    pdf.add_paragraph(
        "In simple terms, PayLens allows anyone on the team to type a question in ordinary, conversational English (for example: "
        "'Why did our success rate drop last week?'). It instantly investigates the underlying payment logs, runs exact calculations, "
        "and produces a clean 'Answer Card' explaining exactly what happened, why it happened, and who needs to fix it."
    )
    
    pdf.add_callout(
        "Core Philosophy: An AI-generated conclusion is dangerous unless the user can audit it. "
        "In PayLens, Python performs 100% of the mathematical calculations; the AI is only used to format the explanation in plain English. "
        "This ensures the AI is structurally incapable of making up numbers or hallucinating percentages.",
        title="THE GOLDEN RULE OF PAYLENS",
        style="info"
    )
    
    # Section 2: The Mystery of the Drop
    pdf.add_section("2", "The Real-World Mystery: Why Did Success Rates Drop?")
    pdf.add_paragraph(
        "The project centers around a famous puzzle in payment analytics called Simpson's Paradox (or Traffic Mix Shift). "
        "Understanding this simple concept makes the entire codebase click into place."
    )
    
    pdf.add_callout(
        "The Grocery Store Analogy: Imagine a supermarket with two checkout options:\n"
        "* Express Self-Checkout: 90% of shoppers finish smoothly.\n"
        "* Customer Service Desk: Handles complex returns and disputes, so only 70% finish smoothly.\n\n"
        "Normally, 90% of customers use Express and 10% use the Service Desk. The store's overall checkout success rate is ~88%.\n\n"
        "Next week, a confusing sign at the store entrance mistakenly directs more customers to the Customer Service Desk. "
        "Now 30% of shoppers end up at the Service Desk instead of 10%!\n\n"
        "Result: The store's overall success rate plunges! A naive manager screams: 'The Customer Service staff got slow and lazy! Fix them!' "
        "In reality, the staff didn't get any slower (their speed is still 70%). The drop happened solely because the traffic mix shifted towards a naturally slower line.",
        title="THE SIMPLE ANALOGY THAT EXPLAINS IT ALL",
        style="success"
    )
    
    pdf.add_paragraph("Here is what actually happened in PayLens's dataset last week:")
    
    headers = ["Payment Method (Rail)", "Traffic Share (Before -> After)", "Individual Success Rate", "Status"]
    rows = [
        ["UPI (Fastest, High Volume)", "61.95% -> 54.95%", "88.87% -> 88.79%", "Healthy (Barely moved)"],
        ["Netbanking (Slower, High Friction)", "8.01% -> 15.02%", "71.26% -> 71.13%", "Healthy (Normal baseline)"],
        ["Cards", "22.01% -> 22.01%", "90.15% -> 90.06%", "Healthy (Flat)"],
        ["Wallets & EMI", "8.03% -> 8.02%", "91.20% -> 91.10%", "Healthy (Flat)"],
    ]
    pdf.add_table(headers, rows, [140, 140, 114, 110])
    
    pdf.add_paragraph(
        "The overall payment success rate dropped by 1.33 percentage points (from 87.93% down to 86.60%). "
        "Notice that neither UPI nor Netbanking got worse! Netbanking was always ~71% successful because Indian bank login portals have naturally higher user drop-offs."
    )
    pdf.add_paragraph(
        "PayLens mathematically proves that 93% of the drop was caused by the traffic shift towards Netbanking. "
        "Instead of waking up the banking infrastructure engineers, PayLens alerts the Checkout and App Routing team: "
        "'Check your mobile app screens -- why did so many customers pick Netbanking instead of UPI last week?'",
        bold_prefix="Actionable Insight:"
    )

    # Section 3: How PayLens Works
    pdf.add_section("3", "How PayLens Works: The 4-Step Pipeline")
    pdf.add_paragraph(
        "When an employee asks PayLens a question, the request travels through a secure, structured pipeline:"
    )
    
    steps = [
        ("Step 1: Understand Question", "Identifies the metric, date window, and comparison period without guessing", "Fast Resolver"),
        ("Step 2: Security & Permissions", "Checks user role. A store owner cannot peek at a competitor's confidential data", "AST Guard"),
        ("Step 3: Database Query & Math", "Executes secure read-only SQL on 777k records; Python calculates exact percentages", "Zero-Hallucination"),
        ("Step 4: Generate Answer Card", "Formats the answer into three strictly separated buckets: Facts, Hypotheses, and Actions", "Auditable UI"),
    ]
    pdf.add_flow_diagram(steps)
    
    pdf.add_subsection("The 3-Part Answer Structure")
    pdf.add_paragraph("PayLens enforces a strict separation in every answer it gives to ensure users never confuse a guess with a fact:")
    pdf.add_bullet("1. Observed Facts", "Strictly what the numbers prove (e.g., 'UPI share fell from 61.9% to 54.9%; Netbanking doubled').")
    pdf.add_bullet("2. Hypotheses", "Plausible explanations labeled clearly as unproven (e.g., '[Medium confidence] A checkout page change or promotional banner favored Netbanking').")
    pdf.add_bullet("3. Recommended Actions", "Clear next steps assigned to specific departments (e.g., '[Routing / Product Team] Audit checkout configuration changes deployed last week').")

    # Section 4: Project Structure
    pdf.add_section("4", "Tour of the Pine Labs Folder: What Lives Where?")
    pdf.add_paragraph("Here is a simple map of what each folder and key file inside this workspace is responsible for:")
    
    pdf.add_bullet("backend/", "The engine of PayLens. Houses the API server, database connectors, security guards, caching layers, and the mathematical decomposition logic.")
    pdf.add_bullet("frontend/", "The web interface. Contains the interactive dashboard where users type questions and view waterfall charts, evidence trails, and data warnings.")
    pdf.add_bullet("data/ & datasets/", "Contains the generator and dataset of 777,757 transactions. It is calibrated against real published NPCI (National Payments Corporation of India) decline benchmarks.")
    pdf.add_bullet("semantic/", "The central dictionary (`semantic_model.yaml`). It explicitly defines what 'Success Rate', 'Gross Volume', and 'Decline' mean so there is never confusion.")
    pdf.add_bullet("eval/ & tests/", "The automated testing suite. Contains 76 tests that verify execution accuracy, data security, and mathematical precision.")
    pdf.add_bullet("README.md & SUBMISSION.md", "Detailed engineering documentation explaining the system design, command-line steps, and validation results.")

    # Section 5: The 6 Failure Scenarios
    pdf.add_section("5", "Edge Cases & Data Quality: 6 Scenarios Handled")
    pdf.add_paragraph(
        "Real-world payment data is never pristine. PayLens intentionally includes and handles 6 common messy data scenarios:"
    )
    
    pdf.add_bullet("1. Traffic Mix Shift", "Correctly identifies when an overall metric drops due to user behavior rather than broken servers.")
    pdf.add_bullet("2. Duplicate Records", "Detects when payment logs were accidentally ingested twice and warns the user without corrupting calculations.")
    pdf.add_bullet("3. Missing Bank Names", "Flags when ~2.2% of transactions lack an issuing bank name and explicitly warns that breakdown rows will not sum to 100%.")
    pdf.add_bullet("4. Settlement Disagreements", "Handles cases where merchant settlement totals disagree with individual transaction logs, applying clear precedence rules.")
    pdf.add_bullet("5. Incomplete Time Windows", "Warns users when queries include today or yesterday if data ingestion is still partially in progress.")
    pdf.add_bullet("6. Unauthorized Peeking", "If Merchant A asks to compare their numbers with Merchant B, PayLens issues an explicit refusal naming the unauthorized entity.")

    # Section 6: How to Run and Try It
    pdf.add_section("6", "How to Run & Experience It")
    pdf.add_paragraph("You can run and test the entire system on your computer without needing external cloud accounts:")
    
    pdf.add_bullet("Step 1: Start Server", "Run `python -m uvicorn backend.api.main:app --port 8000` in your terminal.")
    pdf.add_bullet("Step 2: Open Dashboard", "Open your web browser and go to `http://127.0.0.1:8000`.")
    pdf.add_bullet("Step 3: Try The Headline Question", "Ask: 'Why did our overall success rate drop last week?' and watch PayLens isolate the Netbanking traffic shift.")
    pdf.add_bullet("Step 4: Test Privacy Protections", "Ask: 'Compare my success rate with merchant M-2201' and watch the system protect merchant confidentiality with an explicit refusal.")

    # Section 7: Plain-English Glossary
    pdf.add_section("7", "Plain-English Glossary of Payment Terms")
    pdf.add_paragraph("Payments tech is filled with jargon. Here is what the key terms actually mean:")
    
    pdf.add_bullet("Payment Rail", "The digital highway a payment travels on. Examples include UPI, Cards (Visa/Mastercard), Netbanking, and Wallets.")
    pdf.add_bullet("Success Rate (SR)", "The percentage of transaction attempts that successfully complete without failing or timing out.")
    pdf.add_bullet("NPCI", "National Payments Corporation of India -- the banking umbrella organization that operates UPI and sets Indian payment benchmarks.")
    pdf.add_bullet("Mix Shift (Simpson's Paradox)", "When a company's overall average metric drops because customer behavior moved toward a slower option, even though no technology broke.")
    pdf.add_bullet("Role-Based Access Control (RBAC)", "Security rules ensuring each merchant or user can only see their own store's financial data.")
    pdf.add_bullet("AI Hallucination", "When generic AI tools guess or make up numbers. PayLens prevents this by forcing Python to calculate every number.")

    # Section 8: Frequently Asked Questions
    pdf.add_section("8", "Frequently Asked Questions (FAQ)")
    pdf.add_bullet("Do I need an expensive AI API key to use it?", "No! PayLens includes a built-in deterministic engine that calculates the math, resolves questions, and generates answers completely offline.")
    pdf.add_bullet("Is this using real customer credit cards?", "No. The dataset contains 777,757 synthetic rows calibrated to match real published NPCI banking failure rates, with zero private personal data.")
    pdf.add_bullet("How do I know the AI isn't lying to me?", "Every Answer Card has an 'Evidence & Trace' tab displaying the exact database query and calculation steps so you can audit it yourself.")

    # Summary Callout
    pdf.add_callout(
        "Summary: PayLens transforms complex, messy payment data into clear, trustworthy executive answers. "
        "It eliminates AI hallucinations by letting Python compute the math and confining AI to natural language narration. "
        "Every number you see is traceable, auditable, and secured by multi-layer query guards.",
        title="TAKEAWAY FOR STAKEHOLDERS",
        style="info"
    )

    pdf.finalize()

if __name__ == "__main__":
    build_guide()
