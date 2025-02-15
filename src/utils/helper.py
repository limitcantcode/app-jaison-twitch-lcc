def get_twitch_sub_tier(input_tier):
    return int(input_tier / 1000) # Because for some reason tiers from API are 1000, 2000, 3000