*&---------------------------------------------------------------------*
*& ZCL_LOOP_EXTRACT_HTTP — 既有 RESTful service 抽取 handler
*&
*& SICF 節點建議：/sap/bc/zapi/loop_extract  (Handler: ZCL_LOOP_EXTRACT_HTTP)
*& 也可以掛在 ZCL_MCP_GATE_HANDLER 後面當一個 MCP tool：read_sicf_service
*&
*& GET  ?node=/sap/bc/zapi/po            → 節點 + handler 清單 + 每個 handler 的完整摘要
*& GET  ?class=ZCL_ZAPI_PO_HTTP          → 單一 class 的摘要
*&
*& 回傳 JSON：
*& {
*&   "node": "/sap/bc/zapi/po",
*&   "handlers": [ {
*&     "class": "ZCL_ZAPI_PO_HTTP",
*&     "source": "...完整 class 原始碼（含 implementation）...",
*&     "methods": ["IF_HTTP_EXTENSION~HANDLE_REQUEST", "GET_PO", ...],
*&     "types": [ { "name": "TY_PO_STATUS", "kind": "struct",
*&                  "fields": [ { "name": "EBELN", "type": "C", "len": 10, "ddic": "EBELN", "label": "採購憑證" }, ... ] },
*&                { "name": "TY_ITEM_T", "kind": "table", "line": "TY_ITEM" } ],
*&     "ddic": [ { "name": "ZPO_NOTE", "fields": [...] } ],
*&     "auth_checks": [ { "object": "M_BEST_EKO", "line": 118, "actvt": "'03'" } ],
*&     "status_codes": [ { "code": 404, "line": 92 }, ... ],
*&     "form_fields": [ "ebeln", "with_items" ],
*&     "header_fields": [ "~path_info", "~request_method" ],
*&     "tables_read": ["EKKO", "EKPO", "EKET"],
*&     "tables_written": ["ZPO_NOTE"],
*&     "bapis": ["BAPI_PO_GETDETAIL1"],
*&     "messages": [ { "class": "ZMM_BOND", "number": "012", "line": 130 } ],
*&     "unit_tests": [ "LTC_PO_HTTP" ]
*&   } ]
*& }
*&---------------------------------------------------------------------*
CLASS zcl_loop_extract_http DEFINITION PUBLIC FINAL CREATE PUBLIC.
  PUBLIC SECTION.
    INTERFACES if_http_extension.

  PRIVATE SECTION.
    TYPES: BEGIN OF ty_field,
             name  TYPE string,
             type  TYPE string,
             len   TYPE i,
             dec   TYPE i,
             ddic  TYPE string,
             label TYPE string,
           END OF ty_field,
           ty_field_t TYPE STANDARD TABLE OF ty_field WITH DEFAULT KEY,
           BEGIN OF ty_type,
             name   TYPE string,
             kind   TYPE string,            " struct | table | elem
             line   TYPE string,            " table 的行型別
             fields TYPE ty_field_t,
           END OF ty_type,
           ty_type_t TYPE STANDARD TABLE OF ty_type WITH DEFAULT KEY,
           BEGIN OF ty_auth,
             object TYPE string,
             line   TYPE i,
             actvt  TYPE string,
           END OF ty_auth,
           BEGIN OF ty_status,
             code TYPE i,
             line TYPE i,
           END OF ty_status,
           BEGIN OF ty_msg,
             class  TYPE string,
             number TYPE string,
             line   TYPE i,
           END OF ty_msg,
           BEGIN OF ty_handler,
             class          TYPE string,
             source         TYPE string,
             methods        TYPE string_table,
             types          TYPE ty_type_t,
             ddic           TYPE ty_type_t,
             auth_checks    TYPE STANDARD TABLE OF ty_auth WITH DEFAULT KEY,
             status_codes   TYPE STANDARD TABLE OF ty_status WITH DEFAULT KEY,
             form_fields    TYPE string_table,
             header_fields  TYPE string_table,
             tables_read    TYPE string_table,
             tables_written TYPE string_table,
             bapis          TYPE string_table,
             messages       TYPE STANDARD TABLE OF ty_msg WITH DEFAULT KEY,
             unit_tests     TYPE string_table,
           END OF ty_handler,
           BEGIN OF ty_result,
             node     TYPE string,
             handlers TYPE STANDARD TABLE OF ty_handler WITH DEFAULT KEY,
             error    TYPE string,
           END OF ty_result.

    METHODS handlers_of_node IMPORTING iv_node TYPE string RETURNING VALUE(rt_classes) TYPE string_table.
    METHODS analyse_class    IMPORTING iv_class TYPE string RETURNING VALUE(rs_handler) TYPE ty_handler.
    METHODS read_source      IMPORTING iv_class TYPE string RETURNING VALUE(rt_src) TYPE string_table.
    METHODS scan_source      IMPORTING it_src TYPE string_table CHANGING cs_h TYPE ty_handler.
    METHODS expand_types     IMPORTING iv_class TYPE string it_src TYPE string_table CHANGING cs_h TYPE ty_handler.
    METHODS describe_struct  IMPORTING io_descr TYPE REF TO cl_abap_structdescr RETURNING VALUE(rt_fields) TYPE ty_field_t.
    METHODS add_unique       IMPORTING iv_value TYPE string CHANGING ct_tab TYPE string_table.
    METHODS respond          IMPORTING io_server TYPE REF TO if_http_server is_result TYPE ty_result iv_status TYPE i DEFAULT 200.
ENDCLASS.


CLASS zcl_loop_extract_http IMPLEMENTATION.

  METHOD if_http_extension~handle_request.
    DATA ls_result TYPE ty_result.
    DATA(lv_node)  = to_lower( server->request->get_form_field( 'node' ) ).
    DATA(lv_class) = to_upper( server->request->get_form_field( 'class' ) ).

    IF lv_node IS INITIAL AND lv_class IS INITIAL.
      ls_result-error = 'need ?node=/sap/bc/... or ?class=ZCL_...'.
      respond( io_server = server is_result = ls_result iv_status = 400 ).
      RETURN.
    ENDIF.

    DATA lt_classes TYPE string_table.
    IF lv_node IS NOT INITIAL.
      ls_result-node = lv_node.
      lt_classes = handlers_of_node( lv_node ).
      IF lt_classes IS INITIAL.
        ls_result-error = |SICF node { lv_node } not found or has no handler|.
        respond( io_server = server is_result = ls_result iv_status = 404 ).
        RETURN.
      ENDIF.
    ELSE.
      APPEND lv_class TO lt_classes.
    ENDIF.

    LOOP AT lt_classes INTO DATA(lv_cls).
      APPEND analyse_class( lv_cls ) TO ls_result-handlers.
    ENDLOOP.
    respond( io_server = server is_result = ls_result ).
  ENDMETHOD.


  METHOD handlers_of_node.
    " ICFSERVICE 以 icf_name 分層存放；用路徑最後一段 + 父節點比對最穩，這裡先用最後一段
    DATA(lv_last) = substring_after( val = iv_node sub = '/' occ = -1 ).  " 最後一段
    IF lv_last IS INITIAL. lv_last = iv_node. ENDIF.
    SELECT h~icfhandler
      FROM icfservice AS s
      INNER JOIN icfhandler AS h ON h~icf_name = s~icf_name AND h~icfparguid = s~icfparguid
      WHERE s~icf_name = @lv_last
      INTO TABLE @DATA(lt_h).
    LOOP AT lt_h INTO DATA(ls_h).
      add_unique( EXPORTING iv_value = CONV #( ls_h-icfhandler ) CHANGING ct_tab = rt_classes ).
    ENDLOOP.
  ENDMETHOD.


  METHOD analyse_class.
    rs_handler-class = iv_class.
    DATA(lt_src) = read_source( iv_class ).
    rs_handler-source = concat_lines_of( table = lt_src sep = cl_abap_char_utilities=>newline ).
    scan_source( EXPORTING it_src = lt_src CHANGING cs_h = rs_handler ).
    expand_types( EXPORTING iv_class = iv_class it_src = lt_src CHANGING cs_h = rs_handler ).
  ENDMETHOD.


  METHOD read_source.
    " 定義 + 實作 + 測試 include 全部串起來
    DATA(lo_cls) = cl_oo_object=>get_instance( CONV seoclsname( iv_class ) ).
    DATA lt_includes TYPE seop_methods_w_include.
    DATA lt_lines TYPE string_table.

    " 1) public/protected/private section + method implementations
    DATA(lo_src) = NEW cl_oo_source( clskey = VALUE seoclskey( clsname = iv_class ) ).
    lo_src->read( 'A' ).
    rt_src = lo_src->get_old_source( ).
    " 2) local test classes (CCAU include)
    DATA(lv_incl) = cl_oo_classname_service=>get_ccau_name( CONV seoclsname( iv_class ) ).
    READ REPORT lv_incl INTO lt_lines.
    IF sy-subrc = 0 AND lt_lines IS NOT INITIAL.
      APPEND '" ===== LOCAL TEST CLASSES (CCAU) =====' TO rt_src.
      APPEND LINES OF lt_lines TO rt_src.
    ENDIF.
  ENDMETHOD.


  METHOD scan_source.
    " 純文字掃描；夠拿來反推端點表與欄位表的草稿，精細判讀交給 agent
    DATA lv_line TYPE i.
    LOOP AT it_src INTO DATA(lv_s).
      lv_line = sy-tabix.
      DATA(lv_u) = to_upper( condense( lv_s ) ).
      IF lv_u IS INITIAL OR lv_u(1) = '*' OR lv_u(1) = '"'. CONTINUE. ENDIF.

      " methods
      IF lv_u CP 'METHOD *' AND lv_u NP 'METHODS *'.
        add_unique( EXPORTING iv_value = substring_after( val = lv_u sub = 'METHOD ' ) CHANGING ct_tab = cs_h-methods ).
      ENDIF.
      " local test class
      IF lv_u CP 'CLASS * FOR TESTING*'.
        add_unique( EXPORTING iv_value = substring_before( val = substring_after( val = lv_u sub = 'CLASS ' ) sub = ' ' ) CHANGING ct_tab = cs_h-unit_tests ).
      ENDIF.
      " authority
      IF lv_u CP 'AUTHORITY-CHECK OBJECT*'.
        DATA(lv_obj) = substring_before( val = substring_after( val = lv_u sub = `OBJECT '` ) sub = `'` ).
        APPEND VALUE #( object = lv_obj line = lv_line ) TO cs_h-auth_checks.
      ENDIF.
      IF lv_u CP `*ID 'ACTVT' FIELD*` AND cs_h-auth_checks IS NOT INITIAL.
        DATA(lv_idx) = lines( cs_h-auth_checks ).
        cs_h-auth_checks[ lv_idx ]-actvt = condense( substring_after( val = lv_u sub = 'FIELD' ) ).
      ENDIF.
      " http status
      IF lv_u CP '*SET_STATUS(*'.
        FIND REGEX 'CODE\s*=\s*(\d{3})' IN lv_u SUBMATCHES DATA(lv_code).
        IF sy-subrc = 0.
          APPEND VALUE #( code = lv_code line = lv_line ) TO cs_h-status_codes.
        ENDIF.
      ENDIF.
      " form / header fields
      FIND REGEX `GET_FORM_FIELD\(\s*'([^']+)'` IN lv_u SUBMATCHES DATA(lv_ff).
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = to_lower( lv_ff ) CHANGING ct_tab = cs_h-form_fields ). ENDIF.
      FIND REGEX `GET_HEADER_FIELD\(\s*'([^']+)'` IN lv_u SUBMATCHES DATA(lv_hf).
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = to_lower( lv_hf ) CHANGING ct_tab = cs_h-header_fields ). ENDIF.
      " tables
      FIND REGEX `\bFROM\s+(Z\w+|EK\w+|MAR\w+|LF\w+|T\d\w+|\w+)\b` IN lv_u SUBMATCHES DATA(lv_tab).
      IF sy-subrc = 0 AND ( lv_u CP 'SELECT*' OR lv_u CP '*FROM *' ) AND lv_u NP '*FROM SCREEN*'.
        add_unique( EXPORTING iv_value = lv_tab CHANGING ct_tab = cs_h-tables_read ).
      ENDIF.
      FIND REGEX `^(INSERT|UPDATE|MODIFY|DELETE)\s+(FROM\s+)?(Z\w+)` IN lv_u SUBMATCHES DATA(lv_op) DATA(lv_dummy) DATA(lv_wt).
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = lv_wt CHANGING ct_tab = cs_h-tables_written ). ENDIF.
      " bapis / function modules
      FIND REGEX `CALL FUNCTION\s+'([^']+)'` IN lv_u SUBMATCHES DATA(lv_fm).
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = lv_fm CHANGING ct_tab = cs_h-bapis ). ENDIF.
      " messages
      FIND REGEX `MESSAGE\s+[EWISAX](\d{3})\((\w+)\)` IN lv_u SUBMATCHES DATA(lv_no) DATA(lv_mc).
      IF sy-subrc = 0.
        APPEND VALUE #( class = lv_mc number = lv_no line = lv_line ) TO cs_h-messages.
      ENDIF.
    ENDLOOP.
  ENDMETHOD.


  METHOD expand_types.
    " 用 RTTI 展開 class 內宣告的 TYPES（BEGIN OF ... / TABLE OF ...）以及用到的 DDIC 結構
    DATA lt_names TYPE string_table.
    LOOP AT it_src INTO DATA(lv_s).
      DATA(lv_u) = to_upper( condense( lv_s ) ).
      FIND REGEX `^TYPES[:]?\s+BEGIN OF\s+(\w+)` IN lv_u SUBMATCHES DATA(lv_n).
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = lv_n CHANGING ct_tab = lt_names ). CONTINUE. ENDIF.
      FIND REGEX `^\s*BEGIN OF\s+(\w+)` IN lv_u SUBMATCHES lv_n.
      IF sy-subrc = 0. add_unique( EXPORTING iv_value = lv_n CHANGING ct_tab = lt_names ). CONTINUE. ENDIF.
      FIND REGEX `^(TYPES[:]?\s+)?(\w+)\s+TYPE\s+(STANDARD|SORTED|HASHED)?\s*TABLE OF\s+(\w+)` IN lv_u SUBMATCHES DATA(d1) DATA(lv_tn) DATA(d2) DATA(lv_ln).
      IF sy-subrc = 0.
        APPEND VALUE #( name = lv_tn kind = 'table' line = lv_ln ) TO cs_h-types.
      ENDIF.
      " DDIC structures / tables referenced
      FIND REGEX `TYPE\s+(Z\w+)\b` IN lv_u SUBMATCHES DATA(lv_dd).
      IF sy-subrc = 0 AND lv_u NP '*TABLE OF*'.
        add_unique( EXPORTING iv_value = lv_dd CHANGING ct_tab = lt_names ).
      ENDIF.
    ENDLOOP.

    LOOP AT lt_names INTO DATA(lv_name).
      DATA lo_descr TYPE REF TO cl_abap_typedescr.
      " class-local type: CLASSNAME=>TYPENAME
      cl_abap_typedescr=>describe_by_name( EXPORTING p_name = |{ iv_class }=>{ lv_name }|
                                           RECEIVING p_descr_ref = lo_descr
                                           EXCEPTIONS type_not_found = 1 OTHERS = 2 ).
      DATA(lv_is_ddic) = abap_false.
      IF sy-subrc <> 0.
        cl_abap_typedescr=>describe_by_name( EXPORTING p_name = lv_name
                                             RECEIVING p_descr_ref = lo_descr
                                             EXCEPTIONS type_not_found = 1 OTHERS = 2 ).
        IF sy-subrc <> 0. CONTINUE. ENDIF.
        lv_is_ddic = abap_true.
      ENDIF.
      IF lo_descr->kind = cl_abap_typedescr=>kind_struct.
        DATA(ls_t) = VALUE ty_type( name = lv_name kind = 'struct'
                                     fields = describe_struct( CAST cl_abap_structdescr( lo_descr ) ) ).
        IF lv_is_ddic = abap_true. APPEND ls_t TO cs_h-ddic. ELSE. APPEND ls_t TO cs_h-types. ENDIF.
      ELSEIF lo_descr->kind = cl_abap_typedescr=>kind_table.
        DATA(lo_line) = CAST cl_abap_tabledescr( lo_descr )->get_table_line_type( ).
        APPEND VALUE ty_type( name = lv_name kind = 'table' line = lo_line->get_relative_name( ) ) TO cs_h-types.
      ENDIF.
    ENDLOOP.
  ENDMETHOD.


  METHOD describe_struct.
    DATA(lt_comp) = io_descr->get_ddic_field_list( p_including_substructres = abap_true ).
    IF lt_comp IS NOT INITIAL.
      LOOP AT lt_comp INTO DATA(ls_c).
        APPEND VALUE #( name = ls_c-fieldname type = ls_c-inttype len = ls_c-leng dec = ls_c-decimals
                        ddic = ls_c-rollname label = ls_c-fieldtext ) TO rt_fields.
      ENDLOOP.
      RETURN.
    ENDIF.
    " 非 DDIC 的 local structure
    LOOP AT io_descr->components INTO DATA(ls_k).
      APPEND VALUE #( name = ls_k-name type = ls_k-type_kind len = ls_k-length dec = ls_k-decimals ) TO rt_fields.
    ENDLOOP.
  ENDMETHOD.


  METHOD add_unique.
    IF iv_value IS INITIAL. RETURN. ENDIF.
    IF NOT line_exists( ct_tab[ table_line = iv_value ] ).
      APPEND iv_value TO ct_tab.
    ENDIF.
  ENDMETHOD.


  METHOD respond.
    DATA(lv_json) = /ui2/cl_json=>serialize( data = is_result compress = abap_true pretty_name = /ui2/cl_json=>pretty_mode-low_case ).
    io_server->response->set_status( code = iv_status reason = 'OK' ).
    io_server->response->set_content_type( 'application/json; charset=utf-8' ).
    io_server->response->set_cdata( lv_json ).
  ENDMETHOD.

ENDCLASS.
