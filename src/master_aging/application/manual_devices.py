"""手动录入校验与 Excel 读取；不连接设备、不修改老化计划。"""
import re

from ..adapters.discovery import Discovery


def parse_sns(text):
    tokens = [value.strip() for value in re.split(r'[,\r\n]+', text) if value.strip()]
    if not tokens:
        raise ValueError('请输入至少一个 SN，多个 SN 使用英文逗号或换行分隔。')
    invalid = [sn for sn in tokens if not Discovery.accepts_sn(sn)
               or not re.fullmatch(r'[A-Za-z0-9_-]+', sn)]
    if invalid:
        raise ValueError('以下 SN 格式无效（仅支持 wdse、hisi、orangepi 主控，最多 128 个字符）：' + '、'.join(invalid[:10]))
    return list(dict.fromkeys(tokens))


def read_excel(path):
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    values = []
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            first = next((row for row in rows if any(value is not None for value in row)), None)
            if first is None:
                continue
            headers = [str(value or '').strip().lower().replace(' ', '') for value in first]
            column = next((i for i, header in enumerate(headers)
                           if header in ('sn', '序列号', '设备sn', '主控sn')), None)
            if column is None:
                column = 0
                if first[0] is not None:
                    values.append(str(first[0]))
            for row in rows:
                if column < len(row) and row[column] is not None:
                    values.append(str(row[column]))
    finally:
        workbook.close()
    return parse_sns('\n'.join(values))
