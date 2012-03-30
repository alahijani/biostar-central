"""
Model definitions.

Note: some models are denormalized by design, this greatly simplifies (and speeds up) 
the queries necessary to fetch a certain entry.

"""
import os, random, hashlib, string, difflib, time, re

from django.db import models
from django.db import transaction
from django.contrib.auth.models import User, Group
from django.contrib import admin, messages
from django.conf import settings
from django.db.models import F, Q
from django.core.urlresolvers import reverse

from datetime import datetime, timedelta
from main.server import html, notegen, auth

# import all constants
from main.server.const import *
import markdown

import logging
logger = logging.getLogger(__name__)

class UserProfile( models.Model ):
    """
    Stores user options

    >>> user, flag = User.objects.get_or_create(first_name='Jane', last_name='Doe', username='jane', email='jane')
    >>> prof = user.get_profile()
    """
    # mapping to the Django user
    user  = models.OneToOneField(User, unique=True, related_name='profile')
    
    # user chosen display nam
    display_name  = models.CharField(max_length=250, default='User', null=False,  db_index=True)
    
    # this designates a user as moderator
    type = models.IntegerField(choices=USER_TYPES, default=USER_NEW)
    
    # globally unique id used to identify the user in a private feeds
    uuid = models.TextField(null=False,  db_index=True, unique=True)

    # this is the reputation
    score = models.IntegerField(default=0, blank=True)
    
    # denormalized badge fields to make rendering easier
    bronze_badges = models.IntegerField(default=0)
    silver_badges = models.IntegerField(default=0)
    gold_badges   = models.IntegerField(default=0)
    new_messages  = models.IntegerField(default=0)
   
    # the last visit by the user
    last_visited = models.DateTimeField()
    
    # user status: active, suspended
    status = models.IntegerField(choices=USER_STATUS_TYPES, default=USER_ACTIVE)
    
    # description provided by the user as markup
    about_me = models.TextField(default="", null=True)

    # description provided by the user as html
    about_me_html = models.TextField(default="", null=True)
    
    # user provided location
    location = models.TextField(default="", null=True)
    
    # website may be used as a blog
    website  = models.URLField(default="", null=True, max_length=250)
    
    # description provided by the user as html
    my_tags = models.TextField(default="", null=True, max_length=250)
    
    # google scholar ID
    scholar = models.TextField(null=True, default='', max_length=50)

    @property
    def can_moderate(self):
        return (self.is_moderator or self.is_admin)
      
    @property
    def is_moderator(self):
        return self.type == USER_MODERATOR
    
    @property
    def is_admin(self):
        return self.type == USER_ADMIN
    
    def get_status(self):
        return 'suspended' if self.suspended else ''
    
    @property
    def suspended(self):
        return self.status == USER_SUSPENDED
    
    def get_absolute_url(self):
        return reverse("main.server.views.user_profile", kwargs=dict(uid=self.user.id))

    @property
    def note_count(self):
        note_count = Note.objects.filter(target=self.user).count()
        new_count  = Note.objects.filter(target=self.user, unread=True).count()
        return (note_count, new_count)
    
class Tag(models.Model):
    name  = models.TextField(max_length=50, db_index=True)
    count = models.IntegerField(default=0)
    
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'count')

admin.site.register(Tag, TagAdmin)

class RootPostManager(models.Manager):
    "Used for all posts (question, answer, comment); returns only non-deleted posts"
    def get_query_set(self):
        return super(PostManager, self).get_query_set().select_related('author','author__profile','children', 'descendants')

class AllManager(models.Manager):
    "Returns all posts"
    def get_query_set(self):
        return super(AllManager, self).get_query_set().select_related('author','author__profile')

class OpenManager(models.Manager):
    "Returns all open posts"
    def get_query_set(self):
        return super(OpenManager, self).get_query_set().filter( Q(status=POST_OPEN) | Q(status=POST_CLOSED) ).select_related('author','author__profile')

class Post(models.Model):
    """
    A post is content generated by a user
    """
    
    all_posts  = AllManager()
    open_posts = OpenManager()
    objects    = models.Manager()
    
    # the user that created the post
    author  = models.ForeignKey(User)
    content = models.TextField(null=False, blank=False, max_length=10000) # the underlying Markdown
    html    = models.TextField(blank=True) # this is the sanitized HTML for display
    title   = models.TextField(max_length=200)
    slug    = models.SlugField(blank=True, max_length=200)
    tag_val = models.CharField(max_length=200) # The tag value is the canonical form of the post's tags
    tag_set = models.ManyToManyField(Tag) # The tag set is built from the tag string and used only for fast filtering
    views = models.IntegerField(default=0, blank=True, db_index=True)
    score = models.IntegerField(default=0, blank=True, db_index=True)
    full_score = models.IntegerField(default=0, blank=True, db_index=True)
    
    creation_date = models.DateTimeField(db_index=True)
    lastedit_date = models.DateTimeField()
    lastedit_user = models.ForeignKey(User, related_name='editor')
    
    # post status: active, closed, deleted 
    status = models.IntegerField(choices=POST_STATUS_TYPES, default=POST_OPEN)
    
    # the type of the post
    type = models.IntegerField(choices=POST_TYPES, db_index=True)
    
    # this will maintain the ancestor/descendant relationship bewteen posts
    root = models.ForeignKey('self', related_name="descendants", null=True, blank=True)
    
    # this will maintain parent-child replationships between posts
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children')
       
    # denormalized fields only that only apply to specific cases
    comment_count   = models.IntegerField(default=0, blank=True)
    revision_count  = models.IntegerField(default=0, blank=True)
    answer_count    = models.IntegerField(default=0, blank=True)
    accepted        = models.BooleanField(default=False, blank=True)
   
    # this is used only for blog posts
    url = models.URLField(default='', blank=True)

    # relevance measure, initially by timestamp, other rankings measures
    rank = models.FloatField(default=0, blank=True)
           
    def get_absolute_url(self):
        if self.top_level:
            url = "/post/show/%d/%s/" % (self.root.id, self.root.slug)
        else:
            url = "/post/show/%d/%s/#%d" % (self.root.id, self.root.slug, self.id)
        # some objects have external links
        url  = self.url or url
        return url
          
    def set_tags(self):
        if self.type not in POST_CONTENT_ONLY:
            # save it so that we can set the many2many fiels
            self.tag_set.clear()
            tags = [ Tag.objects.get_or_create(name=name)[0] for name in self.get_tag_names() ]
            self.tag_set.add( *tags )
            self.save()
  
    def get_title(self):
        "Returns the title and a qualifier"
        title = self.title
        if self.deleted:
            title = "%s [deleted ]" % self.title
        elif self.closed:
            title = "%s [closed]" % self.title
        return "%s" % title
               
    @property
    def top_level(self):
        return self.type in POST_TOPLEVEL
        
    @property
    def closed(self):
        return self.status == POST_CLOSED
    
    @property
    def open(self):
        return self.status == POST_OPEN
    
    @property
    def deleted(self):
        return self.status == POST_DELETED

    @property
    def get_status(self):
        "Main status of a post"
        if self.deleted:
            return 'deleted'
        elif self.closed:
            return 'closed'
        else:
            return ''
        
    @property
    def get_label(self):
        "Secondary status of open posts"
        if self.answer_count == 0:
            return 'unanswered'
        elif self.accepted:
            return 'accepted'
        elif self.answer_count:
            return 'answered'
        else:
            return 'open'
               
    def get_tag_names(self):
        "Returns the post's tag values as a list of tag names"
        names = [ html.safe_tag(n) for n in self.tag_val.split() if n ]
        return map(unicode, names)
    
    def apply(self, dir):
        if self.type == POST_ANSWER:
            self.parent.answer_count += dir
            self.parent.save()
        if self.type == POST_COMMENT:
            self.parent.comment_count += dir
            self.parent.save()
    
    def comments(self):
        objs = Post.objects.filter(parent=self, type=POST_COMMENT).select_related('author','author__profile')
        return objs
    
    def combine(self):
        "Returns a compact view that combines all parts of a post. Used in computing diffs between revisions"
        if self.type in POST_CONTENT_ONLY:
            return self.content
        else:
            return "TITLE:%s\n%s\nTAGS:%s" % (self.title, self.content, self.tag_val)

def update_post_views(post, request, amount=3600):
    "Views are updated per user session"
    if request.user.is_anonymous():
        return
    viewed = request.session.get(SESSION_VIEW_COUNT, set())
    if post.id not in viewed:
        # direct updates bypass signals
        Post.objects.filter(id=post.id).update(views = F('views') + 1, rank=F('rank') + amount )
        post.views += 1
        viewed.add(post.id)
        request.session[SESSION_VIEW_COUNT] = viewed
    return post
    
def get_post_manager(user):
    "Returns the right post manager"
    if user and user.is_authenticated() and user.profile.can_moderate:
        return Post.objects
    else:
        return Post.open_posts
    
def query_by_tags(user, text=''):
    "Returns a query by tags"
    posts = get_post_manager(user)
    tags  = re.split("(\+|-)", text)
    active = include = []
    exclude = []
    for tag in tags:
        if tag == '-':
            active = exclude
        elif tag == '+':
            active = include
        elif tag:
            active.append(tag)
    if include:
        res =  posts.filter(type__in=POST_TOPLEVEL, tag_set__name__in=include).exclude(tag_set__name__in=exclude).order_by('-rank').distinct()
    else:
        res =  posts.filter(type__in=POST_TOPLEVEL).exclude(tag_set__name__in=exclude).order_by('-rank').distinct()
    return res

def query_by_mytags(user):
    "Returns a query by the My Tags fields"
    tags  = user.profile.my_tags.split()
    return query_by_tags(user=user, tags=tags)

class Blog(models.Model):
    """
    Sources for Planet feeds
    """
    # the user that blog will belong to
    author  = models.ForeignKey(User)
    url     = models.URLField(max_length=500)

# TODO, not yet used
class Related(models.Model):
    """
    Maintains a relationship between related posts
    """
    source  = models.ForeignKey(Post, related_name="source")
    target  = models.ForeignKey(Post, related_name="target")

# TODO, not yet used
class Visit(models.Model):
    """
    Keeps track of user visits
    """
    source  = models.ForeignKey(User)
    date    = models.DateTimeField(null=False)
    address = models.GenericIPAddressField(default='', null=True, blank=True)
    
# TODO: not yet used, will speed up queries
class PostBody(models.Model):
    """
    Represents the content of a post body.
    It is kept separate to avoid having to retrieve during object queries.
    """
    post    = models.ForeignKey(Post, related_name='bodies')
    content = models.TextField(null=False, blank=False, max_length=10000) # the underlying Markdown
    html    = models.TextField(blank=True) # this is the sanitized HTML for display
   
class PostAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', )

admin.site.register(Post, PostAdmin)

class PostRevision(models.Model):
    """
    Represents various revisions of a single post
    """
    post    = models.ForeignKey(Post, related_name='revisions')
    diff    = models.TextField()    
    content = models.TextField()
    author  = models.ForeignKey(User)
    date    = models.DateTimeField(auto_now=True)
    
    def html(self):
        '''We won't cache the HTML in the DB because revisions are viewed fairly infrequently '''
        return html.generate(self.content)

@transaction.commit_on_success
def user_moderate(user, target, status): 
    """
    Performs a moderator action on a user. 
    """
    if not user.can_moderate:
        msg = 'User %s not a moderator' %user.id
        logger.error(msg)
        return False, msg
    
    if not auth.authorize_user_edit(user=user, target=target, strict=False):
        msg = 'User %s not authorized to moderate %s' % (user.id, target.id)
        logger.error(msg)
        return False, msg

    target.profile.status = status
    target.profile.save()
    text = notegen.user_moderator_action(user=user, target=target)
    send_note(target=target, content=text, sender=user, both=True, type=NOTE_MODERATOR, url=user.get_absolute_url() )

    msg = 'User status set to %s' % target.profile.get_status_display()
    return True, msg
     
@transaction.commit_on_success
def post_moderate(request, post, user, status, date=None):
    """
    Performs a moderator action on the post. 
    """

    # most actions will return the original post
    url = post.get_absolute_url()
    
    # setting posts to open require more than one permission
    if status == POST_OPEN and not user.profile.can_moderate:
        msg = 'User %s not a moderator' %user.id
        messages.error(request, msg) if request else None
        return url
    
    # check that user may write the post
    if not auth.authorize_post_edit(user=user, post=post, strict=False):
        msg = 'User %s may not moderate post %s' %(user.id, post.id)
        messages.error(request, msg) if request else None
        return url
   
    # special treatment for deletion
    no_orphans = (Post.objects.filter(parent=post).exclude(id=post.id).count() == 0)

    # authors may remove their post/comments without a trace as long as it has
    if status == POST_DELETED and no_orphans and (user==post.author):
        # destroy the post with no trace
        Vote.objects.filter(post=post).delete()
        post.delete()
        return "/"
    
    post.status = status
    post.save()
   
    text = notegen.post_moderator_action(user=user, post=post)
    send_note(target=post.author, sender=user, content=text,  type=NOTE_MODERATOR, both=True, url=post.get_absolute_url() )
    
    msg = 'Post status set to %s' % post.get_status_display()
    messages.info(request, msg) if request else None
    
    return url
     
@transaction.commit_on_success        
def send_note(sender, target, content, type=NOTE_USER, unread=True, date=None, both=False, url=''):
    "Sends a note to target"
    date = date or datetime.now()
    url = url[:200]
    Note.objects.create(sender=sender, target=target, content=content, type=NOTE_USER, unread=unread, date=date, url=url)
    if both:
        #send a note to the sender as well
        Note.objects.create(sender=sender, target=sender, content=content, type=type, unread=False, date=date, url=url)

def decorate_posts(posts, user):
    """
    Decorates a queryset so that each returned object has extra attributes.
    For efficiency it mutates the original query. Works well for dozens of objects but
    would need to be reworked for use cases involving more than that.
    """
    if not user.is_authenticated():
        return posts

    pids  = [ post.id for post in posts ]
    votes = Vote.objects.filter(author=user, post__id__in=pids)
    up_votes  = set(vote.post.id for vote in votes if vote.type == VOTE_UP)
    down_votes = set(vote.post.id for vote in votes if vote.type == VOTE_DOWN)
    bookmarks = set(vote.post.id for vote in votes if vote.type == VOTE_BOOKMARK)
    for post in posts :
        post.writeable  = auth.authorize_post_edit(post=post, user=user, strict=False)
        post.upvoted    = post.id in up_votes
        post.downvoted  = post.id in down_votes
        post.bookmarked = post.id in bookmarks
    return posts

@transaction.commit_on_success
def create_revision(post, author=None):
    "Creates a revision from a post. Compares to last content and creates only if the content changed"
    
    author = author or post.author
    last = PostRevision.objects.filter(post=post).order_by('-date')[:1]
    content, author, date = post.combine(), post.lastedit_user, post.lastedit_date
    # compute the unified difference
    prev = last[0].content if last else ''
    if content != prev:
        diff = ''.join(difflib.unified_diff(prev.splitlines(1), content.splitlines(1)))
        rev  = PostRevision.objects.create(diff=diff, content=content, author=author, post=post, date=date)    
        post.revision_count += 1
        post.save()
        #search.update(post=post, created=False)

@transaction.commit_on_success
def post_create_notification(post):
    "Generates notifications to all users related with this post. Invoked only on the creation of the post"
    
    root = post.root or post
    authors = set( [ root.author ] )
    for child in Post.objects.filter(root=root):
        authors.add(child.author)
    
    text = notegen.post_action(user=post.author, post=post)
    
    for target in authors:
        unread = (target != post.author) # the unread flag will be off for the post author        
        send_note(sender=post.author, target=target, content=text, type=NOTE_USER, unread=unread, date=post.creation_date, url=post.get_absolute_url() )
    
    
class Note(models.Model):
    """
    Creates simple notifications that are active until the user deletes them
    """
    sender  = models.ForeignKey(User, related_name="note_sender") # the creator of the notification
    target  = models.ForeignKey(User, related_name="note_target") # the user that will get the note
    content = models.CharField(max_length=5000, default='') # this contains the raw message
    html    = models.CharField(max_length=5000, default='') # this contains the santizied content
    date    = models.DateTimeField(null=False, db_index=True)
    unread  = models.BooleanField(default=True, db_index=True)
    type    = models.IntegerField(choices=NOTE_TYPES, default=NOTE_USER)
    # this is used only for blog posts
    url = models.URLField(default='', blank=True)

    def get_absolute_url(self):
        return "%s" % self.url        

    @property
    def status(self):
        return 'unread' if self.unread else "old"
     
def post_score_change(post, amount, hours=1):
    "How post score changes with votes. Both the rank and the score changes"

    root = post.root
    
    gain = 3600 * hours # the rank increase
    post.rank  += amount * gain
    post.score += amount
    if post == root:
        post.full_score += amount
    post.save()
    
    # different root also needs updating
    if post != root:
        root.full_score += amount
        if post.rank > root.rank:
            root.rank = post.rank
        root.save()
        
    return post, post.root

def user_score_change(user, amount):
    "How user score changes with votes"
    user.profile.score += amount
    user.profile.save()

class Vote(models.Model):
    """
    >>> user, flag = User.objects.get_or_create(first_name='Jane', last_name='Doe', username='jane', email='jane')
    >>> post = Post.objects.create(author=user, type=POST_QUESTION, content='x')
    >>> vote = Vote(author=user, post=post, type=VOTE_UP)
    """
    author = models.ForeignKey(User)
    post = models.ForeignKey(Post, related_name='votes')
    type = models.IntegerField(choices=VOTE_TYPES, db_index=True)
    date = models.DateTimeField(db_index=True, auto_now=True)
    
    def apply(self, dir=1):
        "Applies a score and reputation changes upon a vote. Direction can be set to -1 to undo (ie delete vote)"
        
        post, root = self.post, self.post.root
        if self.type == VOTE_UP:
            post_score_change(post, dir)
            user_score_change(post.author, dir)
        
        if self.type == VOTE_DOWN:
            post_score_change(post, -dir)
            
        if self.type == VOTE_ACCEPT:
            post.accepted = root.accepted = (dir == 1)
            post.save()
            root.save()
            
@transaction.commit_on_success
def insert_vote(post, user, vote_type):
    "Applies a vote. Applying an existing vote type removes it"
    
    # due to race conditions (user spamming vote button) multiple votes may register
    # this removes votes with the metioned type
    votes = Vote.objects.filter(post=post, author=user, type=vote_type)
    if votes:
        vote = votes[0]
        for vote in votes:
            vote.delete()
        msg = '%s removed' % vote.get_type_display()
        logger.info('%s\t%s\t%s' % (user.id, post.id, msg) )
        return vote, msg
    
    # remove opposing votes
    opposing = OPPOSING_VOTES.get(vote_type)
    if opposing:
        for vote in Vote.objects.filter(post=post, author=user, type=opposing):
            vote.delete()
            post = vote.post # this reference now has been changed
        
    vote = Vote.objects.create(post=post, author=user, type=vote_type)
    vote.save()
    msg = '%s added' % vote.get_type_display()
    logger.info('%s\t%s\t%s' % (user.id, post.id, msg) )
    return vote, msg

class Badge(models.Model):
    name = models.CharField(max_length=50)
    description = models.CharField(max_length=200)
    type = models.IntegerField(choices=BADGE_TYPES)
    unique = models.BooleanField(default=False) # Unique badges may be earned only once
    secret = models.BooleanField(default=False) # Secret badges are not listed on the badge list
    count  = models.IntegerField(default=0) # Total number of times awarded
    
    def get_absolute_url(self):
        return "/badge/show/%s/" % self.id

class Award(models.Model):
    '''
    A badge being awarded to a user.Cannot be ManyToManyField
    because some may be earned multiple times
    '''
    badge = models.ForeignKey(Badge)
    user = models.ForeignKey(User)
    date = models.DateTimeField()
    
    def apply(self, dir=1):
        type = self.badge.type
        prof = self.user.get_profile()
        if type == BADGE_BRONZE:
            prof.bronze_badges += dir
        if type == BADGE_SILVER:
            prof.silver_badges += dir
        if type == BADGE_GOLD:
            prof.gold_badges += dir
        prof.save()
        self.badge.count += dir
        self.badge.save()

# most of the site functionality, reputation change
# and voting is auto applied via database signals
#
# data migration will need to route through
# these models (this application) to ensure that all actions
# get applied properly
#
from django.db.models import signals

# Many models have apply() methods that need to be called when they are created
# and called with dir=-1 when deleted to update something.
MODELS_WITH_APPLY = [ Post, Vote, Award ]
    
def apply_instance(sender, instance, created, raw, *args, **kwargs):
    "Applies changes from an instance with an apply() method"
    if created and not raw: # Raw is true when importing from fixtures, in which case votes are already applied
        instance.apply(+1)

def unapply_instance(sender, instance,  *args, **kwargs):
    "Unapplies an instance when it is deleted"
    instance.apply(-1)
    
for model in MODELS_WITH_APPLY:
    signals.post_save.connect(apply_instance, sender=model)
    signals.post_delete.connect(unapply_instance, sender=model)

def make_uuid():
    "Returns a unique id"
    x = random.getrandbits(256)
    u = hashlib.md5(str(x)).hexdigest()
    return u

def create_profile(sender, instance, created, *args, **kwargs):
    "Post save hook for creating user profiles on user save"
    if created:
        uuid = make_uuid() 
        display_name = html.nuke(instance.get_full_name()) or 'Biostar User'
        # push the last_visited into the past so that it the new post counters
        UserProfile.objects.create(user=instance, uuid=uuid, display_name=display_name, last_visited=datetime(2000, 1, 1), about_me='about me')

def update_profile(sender, instance, *args, **kwargs):
    "Pre save hook for profiles"
    instance.about_me_html = html.generate(instance.about_me)
    
from django.template.defaultfilters import slugify

def verify_post(sender, instance, *args, **kwargs):
    "Pre save post information that needs to be applied"
    
    # change type to integer
    instance.type = int(instance.type)

    if not hasattr(instance, 'lastedit_user'):
        instance.lastedit_user = instance.author
    
    # these types must have valid parents
    if instance.type not in POST_TOPLEVEL:
        assert instance.root and instance.parent, "Instance must have parent/root"
         
    instance.creation_date = instance.creation_date or datetime.now()
    instance.lastedit_date = instance.lastedit_date or datetime.now()
    
    # some fields may not be null
    instance.rank = instance.rank or time.mktime(instance.creation_date.timetuple())
    
    # generate a slug for the instance        
    instance.slug = slugify(instance.title)
        
    # generate the HTML from the content
    instance.html = html.generate(instance.content.strip())
            
def finalize_post(sender, instance, created, raw, *args, **kwargs):
    "Post save actions for a post"
    
    if created:
        # ensure that all posts actually have roots/parent
        if not instance.root or not instance.parent or not instance.title:
            instance.root   = instance.root or instance
            instance.parent = instance.parent or instance
            instance.title  = instance.title or ("%s: %s" % (instance.get_type_display()[0], instance.parent.title))
            instance.slug   = slugify(instance.title)
            instance.save()
        
        # when a new post is created all descendants will be notified
        # this is only needed because in stackexchange 1 post creation
        # and content creation are separate steps
        if instance.content and not raw:
            post_create_notification(instance)
            if instance.type != POST_COMMENT:
                create_revision(instance)
                
            # you can turn off indexing from the settings
            if settings.CONTENT_INDEXING:                
                search.update(post=instance, created=created)
                    
def create_award(sender, instance, *args, **kwargs):
    "Pre save award function"
    instance.date = instance.date or datetime.now()
        
def verify_note(sender, instance, *args, **kwargs):
    "Pre save notice function"
    instance.date = instance.date or datetime.now()
    instance.html = html.generate(instance.content)

def finalize_note(sender, instance,created,  *args, **kwargs):
    "Post save notice function"
    if created and instance.unread:
        instance.target.profile.new_messages += 1
        instance.target.profile.save()

def tags_changed(sender, instance, action, pk_set, *args, **kwargs):
    "Applies tag count updates upon post changes"
    if action == 'post_add':
        for pk in pk_set:
            tag = Tag.objects.get(pk=pk)
            tag.count += 1
            tag.save()
    if action == 'post_delete':
        for pk in pk_set:
            tag = Tag.objects.get(pk=pk)
            tag.count -= 1
            tag.save()
    if action == 'pre_clear': # Must be pre so we know what was cleared
        for tag in instance.tag_set.all():
            tag.count -= 1
            tag.save()
            
def tag_created(sender, instance, created, *args, **kwargs):
    "Zero out the count of a newly created Tag instance to avoid double counting in import"
    if created and instance.count != 0:
        # To avoid infinite recursion, we must disconnect the signal temporarily
        signals.post_save.disconnect(tag_created, sender=Tag)
        instance.count = 0
        instance.save()
        signals.post_save.connect(tag_created, sender=Tag)

# now connect all the signals
signals.post_save.connect( create_profile, sender=User )
signals.pre_save.connect( update_profile, sender=UserProfile )

# post signals
signals.pre_save.connect( verify_post, sender=Post )
signals.post_save.connect( finalize_post, sender=Post )

# note signals
signals.pre_save.connect( verify_note, sender=Note )
signals.post_save.connect( finalize_note, sender=Note )

signals.pre_save.connect( create_award, sender=Award )
signals.m2m_changed.connect( tags_changed, sender=Post.tag_set.through )
signals.post_save.connect( tag_created, sender=Tag )

# initializes the search index
from main.server import search
signals.post_syncdb.connect(search.initialize)