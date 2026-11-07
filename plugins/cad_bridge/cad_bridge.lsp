;;; cad_bridge.lsp ——— 智能体集群 CAD 实时桥（纯 AutoLISP，零编译）
;;;
;;; 职责：常驻 AutoCAD 进程，轮询桥目录的指令队列，用 entmake 实时绘制，
;;;       不占用命令行、不锁定界面（用户可随时缩放/平移/编辑）。
;;;       timer 反应器负责空闲轮询，editor 反应器在每次命令结束后追加轮询。
;;;
;;; 加载方式：
;;;   1) 自动：智能体经 COM SendCommand 执行
;;;      (progn (setq *ACB-DIR* "C:/path/to/bridge") (load "C:/path/to/cad_bridge.lsp"))
;;;   2) 手动：APPLOAD 加载本文件 —— 桥目录取自
;;;      %LOCALAPPDATA%\AgentCluster\cad_bridge.dir（锚点文件，Python 负责写入）
;;;
;;; 桥目录结构：
;;;   queue/*.cmd   指令文件（Python 写入，每行一条指令）
;;;   done/*.done   已执行指令（LSP 移入）
;;;   done/*.res    回执（每行 OK|OP|handle 或 ERR|OP|message）
;;;   bridge.heartbeat  心跳（每次轮询重写，Python 读 mtime 判断桥存活）
;;;   bridge.state      桥状态（RUNNING / PAUSED / STOPPED / ERROR:原因）
;;;   control.txt       控制标志（PAUSE / RESUME / STOP，处理后自删）
;;;
;;; 指令协议（行式，竖线分隔，文件用 ANSI 编码，路径用正斜杠）：
;;;   LINE|x1|y1|x2|y2|layer|color
;;;   PLINE|x1,y1 x2,y2 ...|closed(0/1)|layer|color
;;;   CIRCLE|cx|cy|r|layer|color
;;;   ARC|cx|cy|r|a1deg|a2deg|layer|color
;;;   ELLIPSE|cx|cy|majorX|majorY|ratio|layer|color
;;;   TEXT|layer|color|x|y|height|rotation|文本内容（第 8 段起整段为文本，可含竖线）
;;;   DIM|x1|y1|x2|y2|offset|layer|color
;;;   ERASE|handle
;;;   ZOOM|E
;;;   SAVE|
;;;   SAVEAS|path
;;;   READ|

(vl-load-com)

(setq *acb-paused* nil)
(setq *acb-reactor-t* nil)
(setq *acb-reactor-e* nil)

;; ==============================================================
;; 基础工具
;; ==============================================================
(defun acb-split (s sep / pos out)
  (setq out '())
  (while (setq pos (vl-string-search sep s))
    (setq out (cons (substr s 1 pos) out))
    (setq s (substr s (+ pos 1 (strlen sep)))))
  (reverse (cons s out))
)

(defun acb-join (lst)
  (if lst
    (if (cdr lst) (strcat (car lst) "|" (acb-join (cdr lst))) (car lst))
    ""
  )
)

(defun acb-pt2 (s / xy)
  (setq xy (acb-split s ","))
  (list (atof (car xy)) (atof (cadr xy)))
)

(defun acb-dir-join (a b) (strcat a "/" b))

(defun acb-read-file (path / f acc line)
  (setq acc '())
  (if (setq f (open path "r"))
    (progn
      (while (setq line (read-line f)) (setq acc (cons line acc)))
      (close f)))
  (reverse acc)
)

(defun acb-write-file (path lines / f)
  (if (setq f (open path "w"))
    (progn
      (foreach l lines (write-line l f))
      (close f)
      T)
    nil)
)

(defun acb-state (s) (acb-write-file (acb-dir-join *acb-dir* "bridge.state") (list s)))

(defun acb-tail (a i / out n)
  (setq out '() n (length a))
  (while (< i n)
    (setq out (cons (nth i a) out))
    (setq i (+ i 1)))
  (reverse out)
)

;; ==============================================================
;; 图层与绘制（全部 entmake，不占命令行）
;; ==============================================================
(defun acb-ensure-layer (name color)
  (if (and name (/= name "") (not (tblsearch "LAYER" name)))
    (entmake
      (list
        '(0 . "LAYER")
        '(100 . "AcDbSymbolTableRecord")
        '(100 . "AcDbLayerTableRecord")
        (cons 2 name)
        '(70 . 0)
        (cons 62 (if (= color 0) 7 color))
        '(6 . "Continuous")))))

(defun acb-attrs (lay col)
  (progn
    (acb-ensure-layer lay col)
    (append
      (if (and lay (/= lay "")) (list (cons 8 lay)) nil)
      (if (and col (/= col 0)) (list (cons 62 col)) nil))))

(defun acb-handle-of (e / ed)
  (if (and e (setq ed (entget e))) (cdr (assoc 5 ed)) nil)
)

(defun acb-mk (e op / h)
  (if (null e)
    (strcat "ERR|" op "|entmake failed")
    (progn
      (setq h (cond ((= (type e) 'ENAME) (acb-handle-of e))
                    ((= (type e) 'LIST) (cdr (assoc 5 e)))
                    (T nil)))
      (strcat "OK|" op "|" (if h h ""))))
)

(defun acb-d-line (a / lay col)
  (setq lay (nth 4 a) col (atoi (nth 5 a)))
  (acb-mk
    (entmake
      (append
        (list '(0 . "LINE"))
        (acb-attrs lay col)
        (list
          (cons 10 (list (atof (nth 0 a)) (atof (nth 1 a)) 0.0))
          (cons 11 (list (atof (nth 2 a)) (atof (nth 3 a)) 0.0)))))
    "LINE")
)

(defun acb-d-pline (a / pts closed lay col raw p)
  (setq raw (nth 0 a))
  (setq closed (nth 1 a))
  (setq lay (nth 2 a) col (atoi (nth 3 a)))
  (setq pts '())
  (while (/= raw "")
    (setq p (vl-string-search " " raw))
    (if p
      (progn
        (setq pts (cons (acb-pt2 (substr raw 1 p)) pts))
        (setq raw (substr raw (+ p 2))))
      (progn (setq pts (cons (acb-pt2 raw) pts)) (setq raw ""))))
  (setq pts (reverse pts))
  (if (< (length pts) 2)
    "ERR|PLINE|at least 2 points required"
    (acb-mk
      (entmake
        (append
          (list '(0 . "LWPOLYLINE")
                '(100 . "AcDbEntity")
                '(100 . "AcDbPolyline")
                (cons 90 (length pts))
                (cons 70 (if (= closed "1") 1 0)))
          (acb-attrs lay col)
          (mapcar (function (lambda (pt) (cons 10 pt))) pts)))
      "PLINE"))
)

(defun acb-d-circle (a / lay col)
  (setq lay (nth 3 a) col (atoi (nth 4 a)))
  (acb-mk
    (entmake
      (append
        (list '(0 . "CIRCLE"))
        (acb-attrs lay col)
        (list
          (cons 10 (list (atof (nth 0 a)) (atof (nth 1 a)) 0.0))
          (cons 40 (atof (nth 2 a))))))
    "CIRCLE")
)

(defun acb-d-arc (a / lay col)
  (setq lay (nth 5 a) col (atoi (nth 6 a)))
  (acb-mk
    (entmake
      (append
        (list '(0 . "ARC"))
        (acb-attrs lay col)
        (list
          (cons 10 (list (atof (nth 0 a)) (atof (nth 1 a)) 0.0))
          (cons 40 (atof (nth 2 a)))
          (cons 50 (* pi (/ (atof (nth 3 a)) 180.0)))
          (cons 51 (* pi (/ (atof (nth 4 a)) 180.0))))))
    "ARC")
)

(defun acb-d-ellipse (a / lay col)
  (setq lay (nth 5 a) col (atoi (nth 6 a)))
  (acb-mk
    (entmake
      (append
        (list '(0 . "ELLIPSE"))
        (acb-attrs lay col)
        (list
          (cons 10 (list (atof (nth 0 a)) (atof (nth 1 a)) 0.0))
          (cons 11 (list (atof (nth 2 a)) (atof (nth 3 a)) 0.0))
          (cons 40 (atof (nth 4 a))))))
    "ELLIPSE")
)

;; 桥专用文字样式（宋体 TrueType，保证中文字形；Standard 的 txt.shx 无中文字形）
(defun acb-ensure-textstyle ()
  (if (not (tblsearch "STYLE" "ACBBRIDGE"))
    (entmake
      (list
        '(0 . "STYLE")
        '(100 . "AcDbSymbolTableRecord")
        '(100 . "AcDbTextStyleTableRecord")
        (cons 2 "ACBBRIDGE")
        '(70 . 0)
        '(40 . 0.0)
        '(41 . 1.0)
        '(50 . 0.0)
        '(71 . 0)
        '(42 . 2.5)
        (cons 3 "simsun.ttc")
        (cons 4 ""))))
  (princ)
)

(defun acb-bytes (s / lst out)
  (setq lst (vl-string->list s) out "")
  (foreach c lst (setq out (strcat out (itoa c) " ")))
  out
)

;; 文字用 MTEXT 实体：\U+XXXX 转义是 MTEXT 的官方特性（TEXT 单行不保证解析，
;; 真机曾把转义串渲染成 ?）；ACBBRIDGE 宋体样式保证中文字形。
(defun acb-d-text (a / lay col txt)
  (setq lay (nth 0 a) col (atoi (nth 1 a)))
  (setq txt (acb-join (acb-tail a 6)))
  (if (= txt "")
    "ERR|TEXT|empty text"
    (acb-mk
      (entmake
        (append
          (list '(0 . "MTEXT")
                '(100 . "AcDbEntity")
                '(100 . "AcDbMText")
                (cons 1 txt)
                (cons 10 (list (atof (nth 2 a)) (atof (nth 3 a)) 0.0))
                (cons 40 (atof (nth 4 a)))
                '(41 . 0.0)
                '(71 . 1)
                (cons 50 (atof (nth 5 a)))
                (cons 7 "ACBBRIDGE"))
          (acb-attrs lay col)))
      "TEXT"))
)

;; 对齐标注：真机验证 entmake DIMENSION 失败（伪块/样式组码限制），
;; 改用 vla-AddDimAligned（COM 方法，与 zoom/save 同路径，不占命令行）
(defun acb-d-dim (a / x1 y1 x2 y2 off lay col dx dy len nx ny mx my doc ms dim h)
  (setq x1 (atof (nth 0 a)) y1 (atof (nth 1 a)))
  (setq x2 (atof (nth 2 a)) y2 (atof (nth 3 a)))
  (setq off (atof (nth 4 a)) lay (nth 5 a) col (atoi (nth 6 a)))
  (setq dx (- x2 x1) dy (- y2 y1))
  (setq len (sqrt (+ (* dx dx) (* dy dy))))
  (if (= len 0.0) (setq len 1.0))
  (setq nx (/ (- dy) len) ny (/ dx len))
  (setq mx (/ (+ x1 x2) 2.0) my (/ (+ y1 y2) 2.0))
  (setq mx (+ mx (* nx off)) my (+ my (* ny off)))
  (acb-ensure-layer lay col)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
  (setq ms (vla-get-ModelSpace doc))
  (setq dim
    (vla-AddDimAligned ms
      (vlax-3d-point x1 y1 0.0)
      (vlax-3d-point x2 y2 0.0)
      (vlax-3d-point mx my 0.0)))
  (if (and lay (/= lay "")) (vla-put-Layer dim lay))
  (if (/= col 0) (vla-put-Color dim col))
  (setq h (vla-get-Handle dim))
  (strcat "OK|DIM|" h)
)

(defun acb-d-erase (a / e)
  (setq e (handent (nth 0 a)))
  (if e
    (progn (entdel e) (strcat "OK|ERASE|" (nth 0 a)))
    (strcat "ERR|ERASE|no entity with handle " (nth 0 a)))
)

(defun acb-d-zoom (a)
  (vla-ZoomExtents (vlax-get-acad-object))
  "OK|ZOOM|"
)

(defun acb-d-save (a / doc)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
  (vla-Save doc)
  (strcat "OK|SAVE|" (vla-get-FullName doc))
)

(defun acb-d-saveas (a / doc)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
  (vla-SaveAs doc (nth 0 a))
  (strcat "OK|SAVEAS|" (nth 0 a))
)

(defun acb-count-lines (lst / res hit)
  (setq res '())
  (foreach x lst
    (if (setq hit (assoc x res))
      (setq res (subst (list x (+ 1 (cadr hit))) hit res))
      (setq res (cons (list x 1) res))))
  (mapcar
    (function (lambda (x) (strcat "C|" (car x) "|" (itoa (cadr x)))))
    (reverse res))
)

(defun acb-d-read (a / doc ms n cnt txt out)
  (setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
  (setq ms (vla-get-ModelSpace doc))
  (setq cnt '() txt '() n 0)
  (vlax-for o ms
    (setq n (+ n 1))
    (setq objname (vla-get-ObjectName o))
    (setq cnt (cons objname cnt))
    (if (and (or (= objname "AcDbText") (= objname "AcDbMText")) (< (length txt) 50))
      (setq txt (cons (vla-get-TextString o) txt))))
  (setq out (list (strcat "TOTAL|" (itoa n))))
  (setq out (append out (acb-count-lines cnt)))
  (foreach tt txt (setq out (append out (list (strcat "T|" tt)))))
  (strcat "OK|READ|" (acb-join out))
)

;; ==============================================================
;; 指令分发
;; ==============================================================
(defun acb-dispatch (op a)
  (cond
    ((= op "LINE") (acb-d-line a))
    ((= op "PLINE") (acb-d-pline a))
    ((= op "CIRCLE") (acb-d-circle a))
    ((= op "ARC") (acb-d-arc a))
    ((= op "ELLIPSE") (acb-d-ellipse a))
    ((= op "TEXT") (acb-d-text a))
    ((= op "DIM") (acb-d-dim a))
    ((= op "ERASE") (acb-d-erase a))
    ((= op "ZOOM") (acb-d-zoom a))
    ((= op "SAVE") (acb-d-save a))
    ((= op "SAVEAS") (acb-d-saveas a))
    ((= op "READ") (acb-d-read a))
    (T (strcat "ERR|" op "|unknown op")))
)

(defun acb-exec (line / parts op res)
  (setq parts (acb-split line "|"))
  (setq op (strcase (car parts)))
  (setq res (vl-catch-all-apply (function acb-dispatch) (list op (cdr parts))))
  (if (vl-catch-all-error-p res)
    (strcat "ERR|" op "|" (vl-catch-all-error-message res))
    res)
)

;; ==============================================================
;; 队列处理
;; ==============================================================
(defun acb-process-file (fp / lines res-lines line)
  ;; FIX(2026-11-07): read whole file first (handle closed immediately), then dispatch.
  ;; The old version had (close f) INSIDE the while body, so after the first command
  ;; every following (read-line f) raised "file already closed" and the whole drain
  ;; was silently swallowed by vl-catch-all-apply -> queue never drained.
  (setq res-lines '())
  (setq lines (acb-read-file fp))
  (foreach line lines
    (if (> (strlen line) 2)
      (progn
        (setq res-lines (cons (acb-exec line) res-lines))
        (if (= (strcase (substr line 1 5)) "TEXT|")
          (setq res-lines
            (cons
              (strcat "DBG|TEXT|" (acb-bytes (acb-join (acb-tail (acb-split line "|") 7))))
              res-lines))))))
  (reverse res-lines)
)

(defun acb-drain (/ qdir files fp name res)
  ;; FIX(2026-11-07): skip .cmd whose rename fails (locked by another process),
  ;; otherwise the same command would be re-executed on every tick.
  (if (not *acb-skip*) (setq *acb-skip* '()))
  (setq qdir (acb-dir-join *acb-dir* "queue"))
  (setq files (vl-sort (vl-directory-files qdir "*.cmd" 1) '<))
  (while files
    (setq name (car files))
    (if (member name *acb-skip*)
      nil
      (progn
        (setq fp (acb-dir-join qdir name))
        (setq res (acb-process-file fp))
        (acb-write-file
          (acb-dir-join *acb-dir* (strcat "done/" name ".res"))
          (if res res (list "OK|EMPTY|")))
        (if (vl-file-rename fp (acb-dir-join *acb-dir* (strcat "done/" name ".done")))
          nil
          (setq *acb-skip* (cons name *acb-skip*)))))
    (setq files (cdr files)))
  (princ)
)

;; Python 入队后经 COM SendCommand 主动唤醒，指令立即消费（不等 timer tick）
(defun c:ACBWAKE ()
  (if *acb-dir* (acb-drain))
  (princ)
)

;; ==============================================================
;; 心跳与控制
;; ==============================================================
(defun acb-heartbeat (/ f)
  (if (setq f (open (acb-dir-join *acb-dir* "bridge.heartbeat") "w"))
    (progn
      (write-line (rtos (getvar "DATE") 2 12) f)
      (close f)))
  (princ)
)

(defun acb-control (/ cf lines cmd)
  (setq cf (acb-dir-join *acb-dir* "control.txt"))
  (if (findfile cf)
    (progn
      (setq lines (acb-read-file cf))
      (setq cmd (strcase (if lines (car lines) "")))
      (vl-file-delete cf)
      (cond
        ((= cmd "PAUSE") (setq *acb-paused* T) (acb-state "PAUSED"))
        ((= cmd "RESUME") (setq *acb-paused* nil) (acb-state "RUNNING"))
        ((= cmd "STOP")
         (acb-stop-reactors)
         (acb-state "STOPPED")))
      nil)
    nil)
)

;; ==============================================================
;; 主轮询（timer + editor 双反应器，均只在空闲安全点触发）
;; ==============================================================
(defun acb-tick (rea args / stop)
  (if *acb-dir*
    (progn
      (acb-heartbeat)
      (setq stop (vl-catch-all-apply (function acb-control) nil))
      (if (vl-catch-all-error-p stop)
        (acb-state (strcat "ERROR:" (vl-catch-all-error-message stop)))
        (if (not *acb-paused*)
          (vl-catch-all-apply (function acb-drain) nil))))
  )
  (princ)
)

(defun acb-stop-reactors ()
  (if *acb-reactor-t* (vlr-remove *acb-reactor-t*))
  (if *acb-reactor-e* (vlr-remove *acb-reactor-e*))
  (setq *acb-reactor-t* nil *acb-reactor-e* nil)
  (princ)
)

(defun acb-init (/ d f ladir anchor)
  ;; 桥目录确定顺序：
  ;;   1) 外部预设 *ACB-DIR*（SendCommand 加载时由 Python 预设）
  ;;   2) %LOCALAPPDATA%\AgentCluster\cad_bridge.dir 锚点文件（手动 APPLOAD 场景）
  ;;   3) findfile bridge.dir（支持路径里恰好有时的兜底）
  (if (not *acb-dir*)
    (progn
      (setq ladir (getenv "LOCALAPPDATA"))
      (if ladir
        (progn
          (setq anchor (acb-dir-join ladir "AgentCluster/cad_bridge.dir"))
          (if (findfile anchor)
            (progn
              (setq f (open anchor "r"))
              (if f
                (progn
                  (setq *acb-dir* (read-line f))
                  (close f))))))))
  )
  (if (not *acb-dir*)
    (progn
      (setq d (findfile "bridge.dir"))
      (if d
        (progn
          (setq d (vl-filename-directory d))
          (setq f (open (strcat d "/bridge.dir") "r"))
          (if f
            (progn
              (setq *acb-dir* (read-line f))
              (close f)))))))
  (if *acb-dir*
    (progn
      (setq *acb-dir* (vl-string-translate "\\" "/" *acb-dir*))
      (vl-mkdir (acb-dir-join *acb-dir* "queue"))
      (vl-mkdir (acb-dir-join *acb-dir* "done"))
      (acb-ensure-textstyle)
      (acb-stop-reactors)
      (if (member (quote VLR-TIMER-REACTOR) (atoms-family 1)) (setq *acb-reactor-t* (vlr-timer-reactor nil (quote ((:vlr-timer . acb-tick))))) (setq *acb-reactor-t* nil))
      (setq *acb-reactor-e* (vlr-editor-reactor nil '((:vlr-commandended . acb-tick))))
      (setq *acb-paused* nil)
      (acb-state "RUNNING")
      (acb-tick nil nil)
      (princ (strcat "\n[CAD桥] 已启动，轮询目录：" *acb-dir*)))
    (princ "\n[CAD桥] 错误：未找到桥目录（缺 *ACB-DIR* 预设、锚点文件或 bridge.dir）"))
  (princ)
)

(acb-init)
(princ)