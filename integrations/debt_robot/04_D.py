import os
import tkinter as tk
from tkinter import filedialog, messagebox, Listbox, Scrollbar
from PyPDF2 import PdfReader, PdfWriter
from docx2pdf import convert
from datetime import datetime

# 檢查頁數是否為奇數並新增空白頁
def add_blank_page_if_needed(pdf_writer, pdf_reader):
    if len(pdf_reader.pages) % 2 != 0:
        pdf_writer.add_blank_page()

# 合併單一 PDF 檔案並檢查是否需要新增空白頁
def merge_single_pdf(pdf_writer, pdf_file):
    pdf_reader = PdfReader(pdf_file)
    for page_num in range(len(pdf_reader.pages)):
        pdf_writer.add_page(pdf_reader.pages[page_num])
    # 檢查並新增空白頁（如果需要）
    add_blank_page_if_needed(pdf_writer, pdf_reader)

# 合併PDF檔案
def merge_pdfs(pdf_list, output_pdf):
    pdf_writer = PdfWriter()
    
    for pdf_file in pdf_list:
        merge_single_pdf(pdf_writer, pdf_file)
    
    with open(output_pdf, 'wb') as out_pdf:
        pdf_writer.write(out_pdf)

# 讓使用者選擇PDF或DOCX檔案
def select_files():
    file_paths = filedialog.askopenfilenames(
        title="選擇 PDF 或 DOCX 檔案",
        filetypes=[("PDF or DOCX Files", "*.pdf *.docx")]
    )
    for file_path in file_paths:
        listbox.insert(tk.END, file_path)

# 將DOCX轉換成PDF
def convert_docx_to_pdf(docx_file):
    output_pdf = docx_file.replace(".docx", ".pdf")
    convert(docx_file, output_pdf)
    return output_pdf

# 移動選取的PDF檔案向上
def move_up():
    selection = listbox.curselection()
    if selection and selection[0] > 0:
        index = selection[0]
        file = listbox.get(index)
        listbox.delete(index)
        listbox.insert(index - 1, file)
        listbox.select_set(index - 1)

# 移動選取的PDF檔案向下
def move_down():
    selection = listbox.curselection()
    if selection and selection[0] < listbox.size() - 1:
        index = selection[0]
        file = listbox.get(index)
        listbox.delete(index)
        listbox.insert(index + 1, file)
        listbox.select_set(index + 1)

# 刪除選中的PDF檔案
def remove_file():
    selection = listbox.curselection()
    if selection:
        listbox.delete(selection[0])

# 合併 PDF 或轉換後的 DOCX 並儲存
def merge_and_save():
    file_list = listbox.get(0, tk.END)
    if not file_list:
        messagebox.showerror("錯誤", "沒有選擇任何檔案")
        return

    pdf_list = []
    for file in file_list:
        if file.endswith(".docx"):
            # 將 DOCX 檔案轉換為 PDF
            converted_pdf = convert_docx_to_pdf(file)
            pdf_list.append(converted_pdf)
        else:
            pdf_list.append(file)

    # 取得當前日期，並設定預設檔案名稱
    current_date = datetime.now().strftime("%Y%m%d")
    default_filename = f"{current_date}_消費者債務清理調解聲請狀及附件.pdf"
    
    # 開啟儲存對話框，讓使用者選擇儲存位置
    output_file = filedialog.asksaveasfilename(
        initialfile=default_filename,  # 設定預設檔案名稱
        defaultextension=".pdf",
        filetypes=[("PDF Files", "*.pdf")],
        title="儲存合併的 PDF 檔案"
    )
    
    if output_file:
        merge_pdfs(pdf_list, output_file)
        messagebox.showinfo("成功", f"PDF 檔案已成功合併並儲存至：{output_file}")

# 建立主介面
root = tk.Tk()
root.title("消債蘿蔔特-合併檔案")  # 設定視窗標題

# 選擇PDF或DOCX檔案按鈕
select_button = tk.Button(root, text="選擇 PDF 或 DOCX 檔案", command=select_files)
select_button.pack(pady=5)

# 建立列表顯示選擇的檔案
listbox_frame = tk.Frame(root)
listbox_frame.pack(fill=tk.BOTH, expand=True)

scrollbar = Scrollbar(listbox_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

listbox = Listbox(listbox_frame, selectmode=tk.SINGLE, yscrollcommand=scrollbar.set)
listbox.pack(fill=tk.BOTH, expand=True)

scrollbar.config(command=listbox.yview)

# 移動和刪除按鈕
button_frame = tk.Frame(root)
button_frame.pack()

move_up_button = tk.Button(button_frame, text="上移", command=move_up)
move_up_button.grid(row=0, column=0, padx=5, pady=5)

move_down_button = tk.Button(button_frame, text="下移", command=move_down)
move_down_button.grid(row=0, column=1, padx=5, pady=5)

remove_button = tk.Button(button_frame, text="移除", command=remove_file)
remove_button.grid(row=0, column=2, padx=5, pady=5)

# 合併並儲存按鈕
merge_button = tk.Button(root, text="合併並儲存 PDF", command=merge_and_save)
merge_button.pack(pady=10)

# 啟動主迴圈
root.mainloop()
