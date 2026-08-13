# Supabase 공개정보 후보 스캐너

사용자가 입력한 현재 운영 웹페이지 URL을 일괄 확인하고, Supabase 지문 및 민감정보 형식
후보를 마스킹하여 CSV로 정리합니다. Google·Brave API 키나 카드 등록은 필요 없습니다.

```text
input/urls.txt
  -> 공개 HTML 및 동일 출처 JavaScript 확인
  -> rules/*.json 정규식·문맥 분석
  -> 탐지값 마스킹
  -> output/results.csv
  -> output/batch_summary.csv
```

이 도구는 Supabase API·데이터베이스·Storage를 호출하지 않습니다. 결과는 실제 유출
확정이 아니라 분석자가 검토해야 하는 `REVIEW_REQUIRED` 후보입니다.

## 1. 설치

PowerShell에서 실행합니다.

```powershell
cd C:\darkshund
python -m pip install -r requirements.txt
```

## 2. URL 입력

`input/urls.example.txt`를 `input/urls.txt`로 복사한 뒤 조사할 공개 URL을 한 줄에
하나씩 입력합니다. `#`으로 시작하는 줄과 빈 줄은 무시되며 중복 URL은 제거됩니다.

```powershell
Copy-Item .\input\urls.example.txt .\input\urls.txt
```

```text
https://example.com
https://app.example.org
```

처음에는 1~3개의 URL만 넣어 시험하는 것을 권장합니다.

## 3. 실행

```powershell
python supabase_url_scanner.py
```

실행이 끝나면 다음 파일이 생성됩니다.

- `output/results.csv`: 탐지 항목별 상세 결과
- `output/batch_summary.csv`: URL별 탐지 건수와 최고 위험도 요약

두 CSV는 `utf-8-sig`이므로 Windows Excel에서 바로 열 수 있습니다.

CSV는 실행할 때마다 덮어쓰지 않습니다. 기존 결과를 읽고 신규 URL의 결과만 누적하며,
이미 `CLEAN`, `SUPABASE_ONLY`, `REVIEW_REQUIRED`, `SKIPPED`로 완료된 URL은 자동으로
건너뜁니다. `ERROR`는 다음 실행에서 자동 재시도합니다.

기존 완료 URL까지 다시 검사하려면:

```powershell
python supabase_url_scanner.py --rescan
```

강제 재검사에서도 상세 결과는 `evidence_hash` 기준으로 중복 제거되고, URL별 요약은
가장 최근 검사 결과 한 행으로 갱신됩니다.

CSV가 Excel에서 열려 있으면 Windows가 파일을 잠글 수 있습니다. 새 URL을 검사하기
전에는 `results.csv`와 `batch_summary.csv`를 Excel에서 닫아주세요. 저장은 임시 파일을
완성한 뒤 교체하는 방식이라 저장 오류가 나도 기존 CSV를 보존합니다.

## 4. 결과 해석

`batch_summary.csv`의 주요 값:

- `CLEAN`: 현재 규칙으로 탐지된 항목 없음
- `REVIEW_REQUIRED`: 후보가 있어 사람의 검토 필요
- `LOW_CONFIDENCE_MATCHES`: 형식은 일치하지만 모두 LOW 신뢰도라 오탐 가능성이 높음
- `SKIPPED`: robots.txt, URL 형식 또는 콘텐츠 유형으로 검사하지 않음
- `ERROR`: 공개 페이지 요청 실패
- `service_role_candidate=Y`: 공개 JWT의 role이 `service_role`로 판독된 긴급 검토 후보
- `secret_key_candidate=Y`: `sb_secret_`, 민감 환경변수 할당 또는 DB 연결 문자열 후보
- `supabase_detected=Y`: 공개 페이지 또는 동일 출처 JS에서 Supabase 프로젝트 URL 발견
- `SUPABASE_ONLY`: Supabase 지문만 있고 현재 규칙상 민감정보 후보는 없음

요약 CSV에서 무엇을 확인할지도 바로 볼 수 있습니다.

- `detected_types`: 탐지된 유형 목록
- `type_counts`: 유형별 탐지 개수(예: `EMAIL=6; PHONE_NUMBER=41`)
- `confidence_counts`: 최종 신뢰도별 개수
- `review_priority`: `HIGH`, `MEDIUM`, `LOW`, `NONE`
- `review_reason`: 해당 상태가 된 직접적인 이유
- `analyst_next_step`: 상세 CSV에서 다음으로 확인할 사항

`results.csv`에는 유형, 위험도, 기본·최종 신뢰도, 마스킹된 값, 마스킹된 문맥,
문맥 점수 및 증거 해시가 기록됩니다. 원본 키나 토큰은 저장하지 않습니다.

`anon`/publishable 키는 Supabase 클라이언트의 정상 구성일 수 있으므로 발견 자체가
취약점 또는 개인정보 유출을 의미하지 않습니다.

## 5. 탐지 규칙

기본 규칙은 [rules/supabase.json](C:/darkshund/rules/supabase.json)에 있습니다.

- Supabase 프로젝트 URL
- `sb_publishable_`(정상 공개 가능) 및 `sb_secret_`(비공개 필수)
- JWT 및 Supabase role
- 민감 환경변수 할당과 Postgres 연결 문자열
- Bearer token
- Private Key 헤더
- 이메일·대한민국 휴대전화 번호
- IPv4 주소

규칙마다 위험도, 기본 신뢰도, 긍정·부정·강한 부정 문맥 키워드를 지정합니다.

## 6. 테스트

```powershell
python -m unittest discover -s . -p "test_supabase_url_scanner.py" -v
```

## 7. 이후 자동화

시범 운영으로 규칙과 오탐을 먼저 조정한 뒤 다음 단계에서 입력을 자동화할 수 있습니다.

- Google Alerts에서 받은 후보 URL 가져오기
- 허용된 검색 API 연동
- 정기 실행을 위한 Windows 작업 스케줄러 등록

자동 후보 수집을 추가하더라도 URL 검사·마스킹·CSV 출력 부분은 그대로 재사용합니다.
