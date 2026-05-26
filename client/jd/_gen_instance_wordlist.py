"""One-off helper: regenerate instance_names_wordlist.txt (>=500 names)."""
from __future__ import annotations

import re
from pathlib import Path

INSTANCE_NAME_RE = re.compile(r"^[a-z]{1,6}$")

# Curated + expanded short English nouns / labels (lowercase, max 6 letters).
_RAW = """
aardvark abacus abbey able acorn actor adobe adult afar after again agile
agony agree ahead aisle alarm album alert alien alley alloy aloe alpha altar
amber amigo amino among angel anger angle angry ankle annex anvil apart
apple april apron arbor arena argon argue arid armor aroma arrow arson
ashen aside asset atlas attic audio audit auger aural avail avert awake
award axiom azure bacon badge bagel bait baker balmy banjo barge barley
basil basin batch baton bayou beach beacon beard beast beech begin beige
belly bench berry birch birth black blade blame bland blank blast blaze
bleak bless blimp blind blink bliss block blood bloom bluff blunt blush
board boast bobby bodge boggy bogus bolas bolt bond bonus booby boost booth
booty borax bossy botch bound bowed boxer brace braid brain brake brand
brass brave bravo bread break breed brick bride brief brine bring brisk
broad broil broke brook broom broth brown brush brute buddy buggy build
bulge bulky bully bunch bunny burst busby busty butch butte buyer cabal
cabin cable cacao cache cacti caddy cadet cagey cairn camel cameo canal
candy canoe canon caper capon carat cargo carol carry carve casey cask
caste catch cater caulk cause cavil cedar cello chain chair chalk champ
chant chaos chard charm chart chase chasm cheap cheat check cheek cheer
chess chest chick chief child chili chill chime china chirp choke chord
chore chose chuck chump chunk churn cider cigar cinch circa civic civil
claim clamp clang clank claps clash clasp class clave clean clear cleat
clerk click cliff climb cling clip cloak clock clone close cloth cloud
clout clown cluck clutch coach coast cobra cocoa coconut codex coffee
cogent coil coin cola colon color comet comic comma conch condo conic
coral corer corny couch cough count coupe court coven cover covet cower
crack craft cramp crane crank crash crate crave crawl craze crazy creak
cream credo creed creek creep crest crewe crew crisp croak crock crone
crony crook crop cross croup crowd crown crude cruel crumb crush crust
crypt cubic cuddy cumin curio curly curry curse curve curvy cutie cycle
cynic daddy daily dairy daisy dally dance dandy datum daunt deacon decal
decay decor decoy defer deign deity delay delta delve demon denim dense
dental depot depth derby deter detox devil diary dicey digit dingo diner
dingy diode dirge dirty disco ditch ditto ditty divan diver dizzy dodge
dogma doing dolly donor donut doodle door dorky dorm dotty doubt dough
dowel dowry dozen draft drain drake drama drank drape drawl dread dream
dress dried drift drill drink drive droit droll drone drool droop dross
drove drown druid drunk dryad dryer dryly dual duck duct duet duke dull
dully dummy dumpy dunce dune dungy dusky dusty dutch dwarf dwell dwelt
eager eagle early earth eater ebony eclat edict edify eerie eject elbow
elder elect elegy elfin elide elite elope elude email embed ember emcee
empty enact endow enemy enjoy ensue enter envoy epoch epoxy equal equip
erase erect erode error erupt essay ether ethic ethos etude evade event
every evict evoke exact exalt excel exert exile exist expel extol extra
eyrie fable facet faint fairy faith false fancy fanny farce fatal fatty
fault fauna favor fealty feign feint fella femur fence feral ferry fetch
fetid fever fewer fiber fibre field fiend fiery fifth fifty fight filch
filet filly filmy filth final finch finer fiona fire firm first fishy
fitch fitly five fixer fjord flack flail flair flake flame flank flare
flash flask flat flaw flax fleck fleet flesh flick flier fling flint
flirt float flock flood floor flora floss flour flout flown fluff fluid
fluke flume flung flank flunk flush flute flyer foamy focal focus foggy
foist folio folly fond font food fool foot foray force forge forgo fork
form fort forum fossil foster foul found fount fourth fowl fox foxes
foyer frail frame frank fraud freak freed freer fresh fret friar fried
frill frisk fritz frock frog front frost froth frown froze fruit fudge
fugue fully fund fungi funky funny furor fury fuse fuss futon fuzzy
gable gaffe gaily gains galaxy galea gales gamin gamma gamut gassy gator
gaudy gauge gaunt gavel gawky gayer gayly gazer gecko geeky geese genie
genre genoa ghost giant giddy gift gild gill gimme gipsy girth given
gizmo glade gland glare glass glaze gleam glean glide glint gloat glob
gloom glory gloss glove glow glyph gnarl gnash gnome goad goal goat godly
going gold golly goner gong good goofy goose gorge gorse gouge gourd
grace grade graft grain grand grant grape graph grasp grass grave gravy
graze great greed green greet grief grill grim grin grind gripe groan
groin groom grope gross group grout grove growl grown grub gruel gruff
grunt guard guava guess guest guide guild guile guilt guise gulch gully
gumbo gummy guppy gusto gusty gypsy habit haiku hairy halve handy happy
hardy harem harpy harry harsh haste hasty hatch hated haunt haven havoc
hazel heady heard heart heath heave heavy hedge hefty heist helix hello
hence heron hertz hilly hind hinge hippo hitch hoard hobby hoist holly
homer honey honor hoof hook hoop hoot horde horn horse hotel hotly hound
house hovel hover howdy human humid humor hunch hunky hurry husky hutch
hydra hyena hyper icily icing icon idiom idler igloo iliac image imbue
impel imply inane inbox incur index indie inept inert infer ingot inked
inlay inner input inset inter intro ionic irate irony islet issue itchy
ivory jaunt jazzy jelly jerky jetty jewel jiffy joint joist joker jolly
joust joyed judge juice juicy jumbo jump junta juror kayak kazoo kebab
keen keep kelp kempt ketch khaki kinky kiosk kitty knack knead knee knelt
knife knock knoll known koala kooky krill label labor laden ladle lagoon
lance lanky lapel lapse large larva laser lasso latch later lathe latte
laugh layer leafy leaky leant learn lease leash least leave ledge leech
leery lefty legal lemur lemon leper level lever levee lever libel licit
liege lifer light liken lilac limbo limit linen liner ling ling ling
links lion lipid lithe liver livid llama loamy lobby local locus lodge
lofty logic login loopy loose lorry loser lotus louse lousy lover lower
loyal lucid lucky luggage lumen lunar lunch lunge lurid lusty lying lymph
lynch lyric macaw macho macro madam madly magic magma maize major maker
mambo mamma mammy manga mango mania manic manor maple march maria marry
marsh mason match matey mauve maxim maybe mayor mealy meant meaty medal
media medic melee melon mercy merge merit merry meson metal meter metro
micro midge midst might milky mimic mince miner minim minor mint minus
mirth miser missy misty miter mixer moan moat moby modal model modem
moist molar moldy money month moody moose moral moron mossy motel motif
motor motto mould mound mount mourn mouse mouth mover movie mowed mucus
muddy muffin muggy mulch mummy munch mural murky mushy music musky musty
myrrh nadir naive naked nanny nasal nasty natal naval navel needy neigh
nerve nervy never newel nicer niche niece night ninja ninny ninth noble
nobly noise noisy nomad north nosey notch novel nudge nurse nutty nylon
nymph oaken oasis obese occur ocean octal octet odder oddly offal offer
often olden olive omega onion onset opera opine optic orbit order organ
other otter ought ounce outer outgo ovary ovate overt ovine ovoid owing
owner oxide ozone paddy pagan paint palet palm panda panel panic pansy
papal paper parer parry parse party pasta paste patch patio patsy patty
pause payee payer peach pearl pedal penal pence penne penny perch peril
perky pesky petal petty phase phial phlox phone photo piano picot picky
piece piety piggy pilot pinch piney pinky pinup pinto piper pique pitch
pithy pivot pixel pixie pizza place plaid plain plait plane plank plant
plash plate plaza plead pleat plied plier plonk plops plot pluck plumb
plume plump plunk plush poach podium point poise poker polar polka polyp
pooch poppy porch poser posit posse pouch pound pouty power prank prawn
preen press price prick pride primp print prior prism privy prize probe
prone prong proof prose proud prove prowl proxy prude prune psalm pubic
pudgy puff pulpy pulse punch pupal pupil puppy puree purge purl purse
pushy putty pygmy quack quaff quail quake qualm quark quart quash quasi
queen queer quell query quest queue quick quiet quill quilt quirk quite
quota quote rabbi rabid racer radar radii radio radon rafts rainy raise
rajah rally ralph ramen ranch randy range rapid raspy ratio ratty raven
rayon razor reach react ready realm rearm rebar rebel rebus rebut recap
recur redid refer regal rehab reign relax relay relic remit renal renew
repay repel reply rerun reset resin retch retro retry revel revue rhyme
rider ridge rifle right rigid rigor rinse risen rival river rivet roach
roast robin robot rocky rodeo rogue roomy roost rotor rouge rough round
rouse route rover rowdy royal ruddy ruder rugby ruler rumba rumor rummy
runic rural rusty saber sadly safer saggy saint salad sally salon salsa
salty salve sandy saner sappy sassy satin satyr sauce saucy sauna saute
savor savvy scald scale scalp scamp scant scare scarf scary scion scoff
scold scoop scope scorn scour scout scowl scram scrap scree screw scrub
scrum scuff scull scurf scute sedan seedy segue seine seize semen sense
sepia serif serum serve setup seven sever sewer shack shade shady shaft
shake shaky shale shall shame shank shape shard share shark sharp shave
shawl shear shed shell shied shift shine shiny shire shirk shirt shoal
shock shone shook shoot shore shorn short shout shown shred shrew shrub
shrug shuck shunt shyly siege sieve sight sigma silky silly sinew singe
siren sissy sixth sixty skate skier skill skimp skirt skulk skull skunk
slack slain slang slant slash slate slave sleek sleep sleet slice slick
slide slime slimy sling slink sloop slope slosh sloth slouch slung slump
slung slurp slush slyly smack small smart smash smear smell smelt smile
smirk smite smith smock smoke smoky smote snack snail snake snaky snare
snarl sneak sneer snide sniff snipe snobb snoop snore snort snout snowy
snuck snuff soapy sober soggy solar solid solve sonar sonic sonny sooty
sorry sound south sower space spade spank spare spark spasm spawn speak
spear speck speed spell spend spent spice spicy spied spiel spike spiky
spill spilt spine spiny spire spite splat splay split spoil spoke spoof
spook spool spoon spore sport spout spray spree sprig spunk spurn spurt
squad squat squaw squeak squid squint squire squirm squirt stack staff
stage staid stain stair stake stale stalk stall stamp stand stank stare
stark start stash state stave stead steak steal steam steed steel steep
steer stein stem stern stick stiff still stilt sting stink stint stock
stoic stoke stole stomp stone stony stood stool stoop store stork storm
story stout stove strap straw stray streak stream street stress strewn
strip strop strut stuck study stuff stump stung stunk stunt style styli
suave sugar suing suite sulky sully sumac sunny super surer surge surly
sushi swain swamp swank swarm swash swath sway swear sweat sweep sweet
swell swept swift swill swine swing swirl swish swoon swoop sword swore
sworn swung synod syrup tabby table tacit tacky taffy taint taken tally
talon tamer tango tangy taper tapir tardy tarot tarry taste tasty tatty
taunt tawny teach teary tease teddy teeth tempo tenet tenor tense tenth
tepee tepid terry terse test text thank theft their theme there these
theta thick thief thigh thine thing think third thong thorn those three
threw throb throw thrum thumb thump thunk thyme tiara tibia tidal tiger
tight tilde timer timid tipsy titan tithe title toast today toddy token
tonal tongs tonic tooth topaz topic torch torso torte total totem touch
tough towel tower toxic toxin trace track tract trade trail train trait
tramp trance trash trawl tread treat trend triad trial tribe trice trick
tried trice trice trill trim trio tripe trite troll troop trope trout
truce truck truer truly trump trunk truss trust truth tryst tubal tubby
tuber tulip tulle tumble tuner tunny turbo tutor twang tweak tweed tweet
twice twill twine twirl twist twixt tying typo ulcer ultra umbra uncle
uncut under undid undue unfed unfit unify union unite unity unlit unmet
unpin unset untie until unwed unzip upper upset urban urged urine usage
usher using usual usurp utter uvula vague valet valid valor value valve
vapid vapor vault vaunt vegan venom venue verge verse verso verve vicar
video vigil vigor villa vinyl viola viper viral virus visit visor vista
vital vivid vixen vocal vodka vogue voice voila vomit vortex voter vouch
vowel vying wacky wafer wager wagon waist waive wake walk wall walnut
waltz warty waste watch water waver waxen weary weave wedge weedy week
weigh weird welch welsh wench whack whale wharf wheat wheel whelp where
which whiff while whine whiny whirl whisk white whole whoop whose wider
widow width wield wight willy wimpy wince winch windy wiper wired wiser
witch witty woken woman women woody wooer wool word work world worry worse
worst worth would wound woven wrack wrath wreak wreck wrest wrier wring
wrist write wrong wrote wrung wryly yacht yahoo year yeast yield young
youth zebra zesty zinc zonal
""".split()

def main() -> None:
    seen: set[str] = set()
    names: list[str] = []
    for w in _RAW:
        w = w.strip().lower()
        if not w or w in seen:
            continue
        if not INSTANCE_NAME_RE.fullmatch(w):
            continue
        seen.add(w)
        names.append(w)
    names.sort()
    if len(names) < 500:
        raise SystemExit(f"only {len(names)} names; need >= 500")
    out = Path(__file__).with_name("instance_names_wordlist.txt")
    out.write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"wrote {len(names)} names to {out.name}")


if __name__ == "__main__":
    main()
