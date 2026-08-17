# Supabase 공개정보 후보 스캐너

공개 배포 웹앱을 Brave Search API로 찾고, 공개 HTML과 동일 출처 JavaScript에서 Supabase 지문 및 민감정보 **후보**를 탐지해 CSV로 누적합니다. 탐지값은 마스킹하며 Supabase API·DB·Storage를 호출하거나 인증을 우회하지 않습니다.

자동 탐지는 유출 확정 판정이 아닙니다. `REVIEW_REQUIRED`는 담당자가 공개 페이지와 마스킹된 문맥을 직접 확인해야 한다는 뜻입니다.

## 1. 설치

Python 3.10 이상과 Brave Search API 키가 필요합니다.

```powershell
cd C:\darkshund
python -m pip install -r requirements.txt
```

## 2. Brave API 키 설정

키는 코드나 Git 저장소에 넣지 말고, 실행할 PowerShell 창의 환경변수로만 설정합니다.

```powershell
$env:BRAVE_API_KEY="여기에-발급받은-실제-키"
```

키 자체를 출력하지 않고 설정 여부만 확인하려면:

```powershell
if ($env:BRAVE_API_KEY) { "키 설정됨" } else { "키 없음" }
```

PowerShell 창을 닫으면 설정은 사라집니다. 키를 채팅·스크린샷·README·`.env`·Python 파일에 붙이지 마세요.

## 3. 내일까지 보고서: 처음부터 실행하는 순서

### 3-1. 준비

1. 열려 있는 `results.csv`, `batch_summary.csv`, `report_candidates.csv`를 Excel에서 모두 닫습니다.
2. 새 PowerShell 창을 엽니다.
3. 아래 세 줄을 차례대로 실행합니다. 두 번째 줄의 따옴표 안에 본인의 Brave 키를 넣습니다.

```powershell
cd C:\darkshund
$env:BRAVE_API_KEY="발급받은-실제-Brave-API-키"
python -m pip install -r requirements.txt
```

4. 키가 설정됐는지만 확인합니다. 화면에 실제 키가 나오면 안 됩니다.

```powershell
if ($env:BRAVE_API_KEY) { "키 설정됨" } else { "키 없음" }
```

### 3-2. 첫 번째 집중 조사

```powershell
python brave_supabase_pipeline.py --tomorrow-mode
```

`--tomorrow-mode`는 Supabase 단서가 포함된 집중 검색어 64개를 사용하고 요청당 최대 20개 결과를
받습니다. 각 검색어에서
새 후보를 최대 2개만 뽑아 특정 검색어·호스팅에 쏠리지 않도록 하며, 새 페이지는 최대 128개까지
검사합니다. 기본 검색은 약 64요청(약 0.32달러)입니다. 정확도를 위해 0건 검색의 자동 완화는
기본적으로 하지 않습니다. 재현율이 더 중요할 때만 `--relax-zero-results`를 명시하세요.

검색어 하나가 끝날 때마다 `input/urls.txt`와 `output/discovery_results.csv`에 체크포인트를 저장합니다.
중간에 `Ctrl+C`로 종료해도 이미 찾은 URL은 보존되며, 다음 실행에서 중복으로 추가하지 않습니다.

진행 로그가 계속 나오므로 완료될 때까지 PowerShell 창을 닫지 않습니다. 후보 페이지와 같은 출처의
JavaScript를 순차 확인하기 때문에 검색 자체보다 페이지 검사가 더 오래 걸릴 수 있습니다.

### 3-3. 결과가 부족할 때 두 번째 조사

첫 실행이 끝난 후 `output/report_candidates.csv`의 후보가 부족하면 Brave의 다음 검색 결과 페이지를
한 번 더 조사합니다.

```powershell
python brave_supabase_pipeline.py --tomorrow-mode --search-offset 1
```

이 실행의 기본 검색도 약 64요청입니다. `--relax-zero-results`를 함께 사용하면 0건 검색식마다
요청이 한 번 더 발생할 수 있습니다. 그래도
부족할 때만 `--search-offset 2`를 한 번 더 사용하세요. 같은 offset을 반복하면 중복 검색 비용이 들기
쉬우므로 권장하지 않습니다.

### 3-4. CSV 확인 순서

1. `output/report_candidates.csv`를 먼저 엽니다.
2. `review_priority`가 `HIGH`인 행부터 확인합니다.
3. `detected_types`, `type_counts`, `review_reason`을 읽습니다.
4. 자세한 마스킹 문맥은 `output/results.csv`에서 같은 `url`을 찾아 확인합니다. `source`에는 탐지된
   공개 HTML 또는 동일 출처 JavaScript의 실제 URL이 기록됩니다.
5. 해당 공개 URL을 일반 브라우저로 열어 현재도 페이지가 존재하는지만 확인합니다.
6. 로그인·회원가입·폼 제출·키 검증·Supabase API 호출은 하지 않습니다.
7. 확인 시각(KST), URL, 마스킹된 유형, 판단 근거, 오탐 가능성을 보고서에 기록합니다.

`report_candidates.csv`에 있다고 실제 유출이 확정된 것은 아닙니다. 아래처럼 구분합니다.

- `HIGH`: 비공개여야 할 키·service role·DB 연결 문자열·Private Key 형식 후보. 즉시 수동 검토
- `MEDIUM`: 이메일·전화번호·토큰 등 민감할 수 있는 후보. 예시 데이터인지 확인
- `LOW`: 테스트 문자열·문서·형식 오탐 가능성이 큼. 보고서에는 한계를 명시
- `SUPABASE_ONLY`: Supabase를 사용하는 정황만 확인. 민감정보 노출 사례로 세지 않음

보고서에는 “유출 확정” 대신 증거 수준에 맞춰 “공개 페이지에서 민감정보 형식 후보가 관찰됨”,
“마스킹 문맥 기준 추가 검토 필요”처럼 작성합니다. `anon`/publishable 키는 정상적으로 공개되는 구성이
가능하므로 그 자체만으로 취약점으로 판정하지 않습니다.

## 4. 평상시 자동 검색 + 스캔

### 밤새 자동 조사하기(추가 비용 상한 약 3.50달러)

현재 `offset 0` 조사가 완료된 상태에서는 `offset 1~9`를 자동 순회하는 야간 모드를 사용할 수
있습니다. 실제 Brave API 요청을 700회로 강제 제한하므로 현재 단가 기준 이번 프로세스의 추가 사용액은
최대 약 3.50달러입니다. 기존 사용분과 Brave 대시보드의 다른 사용량은 프로그램이 알 수 없으므로
실행 전 대시보드 잔액을 확인하세요.

먼저 같은 PowerShell 창에서 키를 설정한 뒤 숨김 실행 스크립트를 한 번 실행합니다.

```powershell
$env:BRAVE_API_KEY="발급받은-실제-Brave-API-키"
powershell -ExecutionPolicy Bypass -File .\start_overnight.ps1
```

시작 후 PowerShell 창은 닫아도 되지만 컴퓨터는 켜져 있고 절전 모드에 들어가지 않아야 합니다.
CSV와 Excel 파일도 닫아두세요. 진행 상태 확인:

```powershell
powershell -ExecutionPolicy Bypass -File .\check_overnight.ps1
```

중지해야 할 때:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop_overnight.ps1
```

요청별 `query`, `offset`, 성공 여부와 결과 수는 `output/search_request_log.csv`에 즉시 저장됩니다.
중단 후 같은 야간 모드를 다시 실행하면 성공한 검색 요청은 건너뛰므로 같은 요청에 비용을 반복해서
사용하지 않습니다.

먼저 1회 시험 실행합니다.

```powershell
python brave_supabase_pipeline.py
```

기본 검색어 12개를 각 1회 호출하고 결과당 최대 10개를 확인하며, 새 URL은 최대 40개까지만 스캐너에 넘깁니다. 이미 수집하거나 스캔한 URL은 중복 처리하지 않습니다.

6시간마다 계속 실행하려면:

```powershell
python brave_supabase_pipeline.py --interval-hours 6
```

PowerShell 창과 컴퓨터가 켜져 있어야 반복됩니다. `Ctrl+C`로 종료할 수 있습니다. 하루 한 번이면 `--interval-hours 24`를 사용하세요.

검색만 하고 페이지 스캔을 생략하려면:

```powershell
python brave_supabase_pipeline.py --discover-only
```

사용량을 더 줄인 시험 실행:

```powershell
python brave_supabase_pipeline.py --max-queries 2 --max-new-urls 5
```

Brave의 현재 Search 요금은 1,000요청당 5달러이고 매달 5달러 크레딧이 자동 적용됩니다. 기본값을 하루 한 번 실행하면 약 360요청/월입니다. 요금과 저장 권한은 변경될 수 있으므로 Brave 대시보드의 현재 플랜을 확인하세요. 프로그램은 검색 결과의 제목·본문·스니펫은 저장하지 않지만 후보 URL은 저장하므로, 사용 플랜이 검색 결과 저장을 허용하는지도 확인해야 합니다.

## 5. 검색어 바꾸기

검색어는 `input/search_queries.txt`에 한 줄씩 있습니다. 누적 결과에서 적중률이 높았던
`supabase.co`, `powered by supabase`, Supabase 인증 문구를 필수 단서로 사용합니다. 예약·주문·포털
같은 기능 문구만으로는 검색하지 않아 일반 앱 잡음을 줄입니다.

내일 보고서용 검색어는 `input/search_queries_tomorrow.txt`에 있습니다. `supabase.co`, Supabase에서
자주 보이는 인증 화면 문구, 예약·주문·재고·고객지원·행사·문서공유 기능을 호스팅 도메인별로
조합하되 모든 기능 검색에 공개 Supabase 단서를 함께 요구합니다. `service_role`, `sb_secret`, 비밀번호,
Private Key 같은 비밀값 자체를 인터넷 전체에서
직접 찾는 검색식은 포함하지 않습니다.

기본 대상은 `lovable.app`, `vercel.app`, `netlify.app`, `pages.dev`, `web.app`, `firebaseapp.com`, `onrender.com`, `railway.app`입니다. 결과가 이 도메인에 해당하고 경로가 `blog`, `docs`, `tutorial`, `template` 등이 아닐 때만 후보로 받습니다. Supabase 사용 여부는 이후 스캐너가 판별합니다.

## 6. 출력 파일

모든 CSV는 Windows Excel 호환 `utf-8-sig`로 기록됩니다.

- `output/discovery_results.csv`: Brave가 처음 발견한 후보 URL과 사용 검색어
- `output/batch_summary.csv`: URL별 판정·탐지 유형·개수·검토 우선순위
- `output/results.csv`: 마스킹된 탐지 항목과 공개 소스 문맥
- `output/report_candidates.csv`: 보고서 검토가 필요한 행만 우선순위순으로 정리
- `output/search_request_log.csv`: 실제 Brave API 요청 장부와 결과 수
- `output/supabase_scan_report.xlsx`: 위 CSV들을 필터 가능한 Excel 표로 통합
- `input/urls.txt`: 발견한 URL 누적 목록(Git에는 올라가지 않음)

Excel에서 CSV를 열어둔 채 실행하면 파일 잠금 오류가 날 수 있으므로 실행 전 닫아주세요.

`supabase_scan_report.xlsx`의 모든 시트는 Excel 표로 만들어져 있습니다. 머리글 오른쪽의 필터 버튼을
눌러 `review_priority`에서 `HIGH`만 선택하거나, `status`, `detected_types`, `domain` 등을 선택·정렬할
수 있습니다. 파일은 각 검색 페이지 검사가 끝날 때마다 최신 누적 CSV 기준으로 다시 생성됩니다.

`batch_summary.csv` 주요 상태:

- `CLEAN`: 현재 규칙으로 후보 없음
- `SUPABASE_ONLY`: Supabase 지문만 있고 민감정보 후보 없음
- `LOW_CONFIDENCE_MATCHES`: 오탐 가능성이 높은 LOW 후보만 있음
- `REVIEW_REQUIRED`: 사람이 확인할 후보가 있음
- `SKIPPED`: robots.txt, URL 형식, 콘텐츠 유형 등의 이유로 제외
- `ERROR`: 공개 페이지 요청 실패, 다음 실행에서 재시도

특히 `detected_types`, `type_counts`, `review_priority`, `review_reason`, `analyst_next_step`을 먼저 확인하세요. `service_role_candidate=Y` 또는 `secret_key_candidate=Y`이면 원본 값을 복사·사용하지 말고, 마스킹된 문맥과 공개 위치만 확인해 운영 주체의 공식 보안 연락처로 제보합니다.

## 7. URL을 직접 넣어 스캔

`input/urls.txt`에 공개 URL을 한 줄씩 넣고 실행합니다.

```powershell
python supabase_url_scanner.py
```

완료된 URL까지 다시 확인하려면:

```powershell
python supabase_url_scanner.py --rescan
```

## 8. 안전 범위

- 공개 HTML과 동일 출처 JavaScript만 읽습니다.
- 로그인, 계정 생성, CAPTCHA 우회, 폼 제출을 하지 않습니다.
- Supabase REST/Auth/Storage/DB 엔드포인트를 시험하지 않습니다.
- 실제 키·토큰·비밀번호를 검증하거나 CSV에 원문 저장하지 않습니다.
- `anon`/publishable 키는 정상 공개 구성일 수 있으므로 발견 자체가 취약점은 아닙니다.
- 조사·보관·제보는 소속 기관의 승인 범위와 해당 서비스 약관을 따라야 합니다.

## 9. 테스트

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```
