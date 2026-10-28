package ta

import "time"

func unix(s int64) time.Time { return time.Unix(s, 0).UTC() }
